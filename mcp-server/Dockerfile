FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sre_tools_server.py .
EXPOSE 8082
CMD ["python", "sre_tools_server.py"]
