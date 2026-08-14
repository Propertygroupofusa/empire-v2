#!/bin/bash
# Empire v2 Server - Direct startup without watchdog
# Railway container handles restart logic

cd /app
python3 main.py
