FROM alpine:latest
RUN apk add --no-cache python3 iproute2
COPY router.py /app/router.py
WORKDIR /app
CMD ["python3", "router.py"]
