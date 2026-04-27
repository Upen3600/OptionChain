
from flask import Flask
from flask_socketio import SocketIO
import os

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

@app.route("/")
def home():
    return "✅ Dashboard Running"

def run_dashboard():
    port = int(os.environ.get("PORT", 8080))
    socketio.run(app, host="0.0.0.0", port=port)

def start_ticker(token, scanner):
    print("Ticker placeholder started")
