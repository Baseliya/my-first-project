FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY budget_agent ./budget_agent
ENV PYTHONUNBUFFERED=1
VOLUME /app/data
CMD ["python", "-m", "budget_agent"]
