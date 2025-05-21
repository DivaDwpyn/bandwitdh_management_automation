from flask import Flask, render_template
from flask_socketio import SocketIO
import paramiko
import time
import threading
from collections import deque

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")

# Konfigurasi MikroTik
MIKROTIK_IP = "10.5.50.1"
MIKROTIK_USER = "admin"
MIKROTIK_PASS = "12345678"
INTERFACE = "ether4"

try:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(MIKROTIK_IP, username=MIKROTIK_USER, password=MIKROTIK_PASS, port=22)

    stdin, stdout, stderr = client.exec_command("/interface print")
    output = stdout.read().decode()

    print("Berhasil koneksi ke MikroTik!")
    print("Output dari MikroTik:")
    print(output)

    client.close()

except Exception as e:
    print("Gagal koneksi ke MikroTik:", e)

rx_history = deque(maxlen=60)
tx_history = deque(maxlen=60)

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

def get_traffic():
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(MIKROTIK_IP, username=MIKROTIK_USER, password=MIKROTIK_PASS, port=22)

    while True:
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

        print(f"[INFO] RX: {rx_rate} bps | TX: {tx_rate} bps | AVG RX: {avg_rx} bps | AVG TX: {avg_tx} bps | DHCP: {connected_devices} perangkat")

        # Kirim data ke frontend
        socketio.emit("update_traffic", {
            "rx": rx_rate,
            "tx": tx_rate,
            "avg_rx": avg_rx,
            "avg_tx": avg_tx,
            "dhcp": connected_devices
        })

        time.sleep(1)

@app.route("/")
def index():
    return render_template("index.html")

@socketio.on("connect")
def handle_connect():
    print("Client terhubung!")

if __name__ == "__main__":
    socketio.start_background_task(get_traffic)
    socketio.run(app, host="0.0.0.0", port=5100, debug=True)
