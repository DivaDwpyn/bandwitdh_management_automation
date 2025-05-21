from flask import Flask, render_template
from flask_socketio import SocketIO
import paramiko
import time
import threading
from collections import deque
import google.generativeai as genai
import json
import logging

# Konfigurasi logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[logging.FileHandler("mikrotik_monitor.log"), 
                              logging.StreamHandler()])
logger = logging.getLogger(__name__)

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# Konfigurasi MikroTik
MIKROTIK_IP = "10.5.50.1"
MIKROTIK_USER = "admin"
MIKROTIK_PASS = "12345678"
INTERFACE = "ether4"
BANDWIDTH_LIMIT = 30  # Mbps
THROTTLE_SPEED = 15    # Mbps

# Konfigurasi Google Gemini
GEMINI_API_KEY = ""
genai.configure(api_key=GEMINI_API_KEY)

# Model Gemini
model = genai.GenerativeModel('gemini-2.0-flash')

# Cache untuk menyimpan perangkat yang sudah dibatasi
throttled_devices = {}

# Inisialisasi koneksi SSH
def get_ssh_client():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(MIKROTIK_IP, username=MIKROTIK_USER, password=MIKROTIK_PASS, port=22)
    return client

try:
    client = get_ssh_client()
    stdin, stdout, stderr = client.exec_command("/interface print")
    output = stdout.read().decode()

    logger.info("Berhasil koneksi ke MikroTik!")
    logger.info(f"Output dari MikroTik:\n{output}")

    client.close()

except Exception as e:
    logger.error(f"Gagal koneksi ke MikroTik: {e}")

rx_history = deque(maxlen=60)
tx_history = deque(maxlen=60)
device_traffic = {}  # Untuk melacak trafik per perangkat

def parse_speed(value):
    value = value.lower().strip()
    if "mbps" in value:
        return float(value.replace("mbps", "")) * 1_000_000
    elif "kbps" in value:
        return float(value.replace("kbps", "")) * 1_000
    elif "bps" in value:
        return float(value.replace("bps", ""))
    else:
        return 0

def get_device_mac_ip_mapping():
    """Mendapatkan mapping perangkat (MAC - IP) dari DHCP lease"""
    client = get_ssh_client()
    stdin, stdout, stderr = client.exec_command("/ip dhcp-server lease print")
    output = stdout.read().decode()
    client.close()
    
    devices = {}
    current_device = {}
    
    for line in output.strip().split("\n"):
        if line.startswith(" "):  # Detail item
            if "=" in line:
                key, value = line.strip().split("=", 1)
                current_device[key.strip()] = value.strip()
        else:  # New item
            if current_device and 'mac-address' in current_device and 'address' in current_device:
                devices[current_device['mac-address']] = {
                    'ip': current_device['address'],
                    'hostname': current_device.get('host-name', 'unknown')
                }
            current_device = {}
    
    # Tambahkan item terakhir
    if current_device and 'mac-address' in current_device and 'address' in current_device:
        devices[current_device['mac-address']] = {
            'ip': current_device['address'],
            'hostname': current_device.get('host-name', 'unknown')
        }
    
    return devices

def get_device_traffic():
    """Mendapatkan trafik per perangkat dengan torch"""
    client = get_ssh_client()
    stdin, stdout, stderr = client.exec_command("/ip firewall connection print")
    connections = stdout.read().decode()
    client.close()
    
    devices = get_device_mac_ip_mapping()
    
    # Map IP ke MAC
    ip_to_mac = {device['ip']: mac for mac, device in devices.items()}
    
    device_traffic = {}
    for mac, device_info in devices.items():
        device_traffic[mac] = {
            'ip': device_info['ip'],
            'hostname': device_info['hostname'],
            'rx': 0,
            'tx': 0
        }
    
    # Jalankan torch untuk mengukur trafik per IP
    client = get_ssh_client()
    for ip in [device['ip'] for device in devices.values()]:
        stdin, stdout, stderr = client.exec_command(f"/tool torch interface={INTERFACE} src-address={ip} once")
        torch_output = stdout.read().decode()
        
        rx, tx = 0, 0
        for line in torch_output.split('\n'):
            if 'rx-bps' in line:
                rx = parse_speed(line.split(':')[1].strip())
            if 'tx-bps' in line:
                tx = parse_speed(line.split(':')[1].strip())
        
        if ip in ip_to_mac:
            mac = ip_to_mac[ip]
            device_traffic[mac]['rx'] = rx
            device_traffic[mac]['tx'] = tx
    
    client.close()
    return device_traffic

def ask_gemini_for_throttling_script(device_info, rx_speed, tx_speed, limit_to):
    """Meminta Gemini membuat script untuk pembatasan bandwidth"""
    prompt = f"""
    Saya perlu script untuk MikroTik RouterOS untuk membatasi bandwidth perangkat:
    
    Detail Perangkat:
    - IP Address: {device_info['ip']}
    - MAC Address: {device_info['mac']}
    - Hostname: {device_info['hostname']}
    - Download saat ini: {rx_speed:.2f} Mbps (melebihi batas {BANDWIDTH_LIMIT} Mbps)
    - Upload saat ini: {tx_speed:.2f} Mbps
    
    Buat script MikroTik RouterOS untuk:
    1. Membuat simple queue untuk device tersebut
    2. Batasi bandwidth menjadi {limit_to} Mbps (max-limit={limit_to}M) untuk upload dan download
    3. Berikan prioritas 8 (terendah)
    4. Beri nama queue "LIMITED-{device_info['ip']}"
    
    Berikan HANYA script RouterOS saja tanpa penjelasan tambahan.
    """
    
    try:
        response = model.generate_content(prompt)
        script = response.text.strip()
        # Bersihkan script dari markdown jika ada
        if script.startswith("```") and script.endswith("```"):
            script = "\n".join(script.split("\n")[1:-1])
        return script
    except Exception as e:
        logger.error(f"Error saat menggunakan Gemini API: {e}")
        # Fallback script jika Gemini gagal
        return f"/queue simple add name=LIMITED-{device_info['ip']} target={device_info['ip']} max-limit={limit_to}M/{limit_to}M priority=8 comment=\"Auto-limited by system\""

def apply_throttling(device_mac, device_info, rx_speed, tx_speed):
    """Menerapkan pembatasan bandwidth menggunakan Gemini untuk generate script"""
    if device_mac in throttled_devices:
        # Sudah dibatasi sebelumnya
        return
    
    logger.info(f"Menerapkan pembatasan pada {device_info['hostname']} ({device_info['ip']}) - RX: {rx_speed:.2f} Mbps")
    
    # Minta Gemini membuat script
    throttling_script = ask_gemini_for_throttling_script(
        {'ip': device_info['ip'], 'mac': device_mac, 'hostname': device_info['hostname']},
        rx_speed, tx_speed, THROTTLE_SPEED
    )
    
    logger.info(f"Script dari Gemini: {throttling_script}")
    
    # Terapkan script ke MikroTik
    try:
        client = get_ssh_client()
        stdin, stdout, stderr = client.exec_command(throttling_script)
        result = stdout.read().decode()
        error = stderr.read().decode()
        client.close()
        
        if error:
            logger.error(f"Error saat menerapkan throttling: {error}")
        else:
            logger.info(f"Berhasil menerapkan throttling: {result}")
            # Tandai perangkat sebagai sudah dibatasi
            throttled_devices[device_mac] = {
                'ip': device_info['ip'],
                'hostname': device_info['hostname'],
                'throttled_at': time.time(),
                'original_speed': max(rx_speed, tx_speed)
            }
            
            # Emit event ke frontend
            socketio.emit("throttling_event", {
                'device': device_info['hostname'],
                'ip': device_info['ip'],
                'mac': device_mac,
                'speed_before': max(rx_speed, tx_speed),
                'limited_to': THROTTLE_SPEED,
                'timestamp': time.time()
            })
            
    except Exception as e:
        logger.error(f"Gagal menerapkan throttling: {e}")

def remove_throttling(device_mac):
    """Menghapus pembatasan bandwidth"""
    if device_mac not in throttled_devices:
        return
    
    device_info = throttled_devices[device_mac]
    
    try:
        client = get_ssh_client()
        script = f"/queue simple remove [find where target={device_info['ip']}]"
        stdin, stdout, stderr = client.exec_command(script)
        result = stdout.read().decode()
        error = stderr.read().decode()
        client.close()
        
        if error:
            logger.error(f"Error saat menghapus throttling: {error}")
        else:
            logger.info(f"Berhasil menghapus throttling untuk {device_info['hostname']} ({device_info['ip']})")
            # Hapus dari daftar perangkat yang dibatasi
            del throttled_devices[device_mac]
            
            # Emit event ke frontend
            socketio.emit("unthrottling_event", {
                'device': device_info['hostname'],
                'ip': device_info['ip'],
                'mac': device_mac,
                'timestamp': time.time()
            })
            
    except Exception as e:
        logger.error(f"Gagal menghapus throttling: {e}")

def check_and_release_throttled_devices():
    """Periksa perangkat yang sudah dibatasi dan lepaskan jika sudah 30 menit"""
    current_time = time.time()
    to_release = []
    
    for mac, info in throttled_devices.items():
        # Lepaskan pembatasan setelah 30 menit
        if current_time - info['throttled_at'] >= 30 * 60:  # 30 menit
            to_release.append(mac)
    
    for mac in to_release:
        logger.info(f"Melepaskan pembatasan untuk {throttled_devices[mac]['hostname']} ({throttled_devices[mac]['ip']})")
        remove_throttling(mac)

def get_traffic():
    client = get_ssh_client()

    while True:
        try:
            # Ambil traffic keseluruhan interface
            stdin, stdout, stderr = client.exec_command(f"/interface monitor-traffic {INTERFACE} once")
            output = stdout.read().decode()

            rx_rate, tx_rate = 0, 0
            for line in output.split("\n"):
                if "rx-bits-per-second" in line:
                    rx_rate = parse_speed(line.split(":")[1].strip())
                if "tx-bits-per-second" in line:
                    tx_rate = parse_speed(line.split(":")[1].strip())

            rx_history.append(rx_rate)
            tx_history.append(tx_rate)

            avg_rx = sum(rx_history) / len(rx_history) if rx_history else 0
            avg_tx = sum(tx_history) / len(tx_history) if tx_history else 0

            # Ambil jumlah perangkat DHCP lease
            stdin, stdout, stderr = client.exec_command(
                "/ip dhcp-server lease print without-paging")
            leases_output = stdout.read().decode()
            active_leases = leases_output.strip().split("\n")
            # Filter baris kosong dan baris yang bukan lease
            connected_devices = len([line for line in active_leases if line and not line.strip().startswith(' ')])

            # Ambil trafik per perangkat
            per_device_traffic = get_device_traffic()
            
            # Check perangkat yang melebihi batas
            for mac, device in per_device_traffic.items():
                rx_mbps = device['rx'] / 1_000_000
                tx_mbps = device['tx'] / 1_000_000
                
                # Jika perangkat melebihi batas, terapkan throttling
                if rx_mbps > BANDWIDTH_LIMIT or tx_mbps > BANDWIDTH_LIMIT:
                    apply_throttling(mac, device, rx_mbps, tx_mbps)
            
            # Periksa perangkat yang sudah dibatasi
            check_and_release_throttled_devices()
            
            logger.info(f"RX: {rx_rate/1_000_000:.2f} Mbps | TX: {tx_rate/1_000_000:.2f} Mbps | AVG RX: {avg_rx/1_000_000:.2f} Mbps | AVG TX: {avg_tx/1_000_000:.2f} Mbps | DHCP: {connected_devices} perangkat")

            # Kirim data ke frontend
            socketio.emit("update_traffic", {
                "rx": rx_rate,
                "tx": tx_rate,
                "avg_rx": avg_rx,
                "avg_tx": avg_tx,
                "dhcp": connected_devices,
                "per_device": per_device_traffic,
                "throttled": list(throttled_devices.keys())
            })

        except Exception as e:
            logger.error(f"Error dalam monitoring: {e}")
            # Reconnect if connection lost
            try:
                client.close()
                client = get_ssh_client()
            except:
                logger.error("Gagal reconnect ke MikroTik")
                time.sleep(5)  # Wait before retry

        time.sleep(1)

@app.route("/")
def index():
    return render_template("index.html")

@socketio.on("connect")
def handle_connect():
    logger.info("Client terhubung!")

@socketio.on("remove_throttling")
def handle_remove_throttling(data):
    """Handler untuk menghapus throttling dari frontend"""
    if 'mac' in data:
        mac = data['mac']
        if mac in throttled_devices:
            logger.info(f"Menghapus throttling untuk {mac} (diminta oleh pengguna)")
            remove_throttling(mac)
            return {"status": "success", "message": f"Throttling dihapus untuk {mac}"}
    return {"status": "error", "message": "MAC address tidak valid atau tidak dibatasi"}

if __name__ == "__main__":
    socketio.start_background_task(get_traffic)
    socketio.run(app, host="0.0.0.0", port=5100, debug=True)