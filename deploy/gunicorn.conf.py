"""
Gunicorn settings sized for a small shared VM (1 CPU).

Requests mostly wait on USDA / Open Food Facts / Gemini rather than using CPU, so threads
give concurrency cheaply: 2 workers x 4 threads = 8 requests in flight at once. Extra
requests wait in the listen backlog; nginx rate limits stop that queue from growing.
"""
import os

bind = "0.0.0.0:8000"
worker_class = "gthread"
workers = int(os.getenv("GUNICORN_WORKERS", 2))
threads = int(os.getenv("GUNICORN_THREADS", 4))

# Longest legitimate request: two Gemini attempts (15s each) + USDA (15s) = 45s. Anything slower is killed.
timeout = 60
graceful_timeout = 20
keepalive = 5
backlog = 64

# Recycle workers now and then so any slow memory growth can't accumulate
max_requests = 1000
max_requests_jitter = 100

accesslog = None  # nginx already logs requests
errorlog = "-"
loglevel = "warning"
