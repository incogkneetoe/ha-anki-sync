FROM python:3.12-slim-bookworm
RUN pip install --no-cache-dir anki==26.9.3
COPY server.py /server.py
CMD ["python", "-u", "/server.py"]
