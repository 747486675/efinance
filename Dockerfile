FROM python:3.12-slim
WORKDIR /app
COPY . .
# Install the checked-out source instead of the unrelated published release.
RUN pip install --no-cache-dir .
CMD ["python"]
