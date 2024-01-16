FROM docker.io/library/python:3.12
WORKDIR /app
COPY earningsbot.py requirements.txt /app/
RUN pip install -r requirements.txt
CMD ["python", "earningsbot.py"]