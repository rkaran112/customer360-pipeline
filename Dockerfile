# Customer 360 as a container: the same image runs on a laptop, a server or any cloud.
#   docker build -t customer360 .
#   docker run -p 8501:8501 customer360        -> open http://localhost:8501
FROM python:3.12-slim

WORKDIR /app

# Install libraries first: Docker caches this layer, so code changes rebuild in seconds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run the pipeline once at build time: if the data or a check is broken, the build fails.
RUN python run_pipeline.py

EXPOSE 8501
CMD ["streamlit", "run", "streamlit_app.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true"]
