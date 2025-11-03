# PiZeroMetarMap
Set up Automated Startup

1️⃣ Create a service file
sudo nano /etc/systemd/system/metarmap.service

#Paste this inside:

[Unit]
Description=METAR Map LED Service
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/METARmap.py  #change to path of python code
WorkingDirectory=/home/pi                        #change to /home/username
Restart=always          
RestartSec=10
User=pi

[Install]
WantedBy=multi-user.target


2️⃣ Enable and start the service
sudo systemctl daemon-reload
sudo systemctl enable metarmap.service
sudo systemctl start metarmap.service

3️⃣ Check that it’s running
systemctl status metarmap.service



Check logs with
journalctl -u metarmap.service -f
