FROM python:3.10-slim

WORKDIR /app
COPY ecorex_admin_api.py /app/ecorex_admin_api.py

ENV ECOREX_ADMIN_DB=/data/ecorex-admin.sqlite3
EXPOSE 18084

CMD ["python", "/app/ecorex_admin_api.py", "--host", "0.0.0.0", "--port", "18084"]
