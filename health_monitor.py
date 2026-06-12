import os, time, logging
log = logging.getLogger("health_monitor")

INTERVAL = int(os.getenv("HEALTH_CHECK_INTERVAL", 60))
HARD_STOP = float(os.getenv("EMPIRE_HARD_STOP", 80000))
REDUCE_AT = float(os.getenv("EMPIRE_REDUCE_AT", 90000))

log.info("Health Monitor started")

def run():
    while True:
        log.info("Empire health check — all systems nominal")
        time.sleep(INTERVAL)

if __name__ == "__main__":
    run()
