# Docker Development Environment

This Dockerfile builds an image suitable for running the test suite on a Linux environment with libiperf (iperf3) installed.

The image is designed for **volume-mounted development**: mount your local code into the container so you can edit files and re-run tests without rebuilding the image.

## Build (one time)

```powershell
# from repository root
docker build -t py-iperf3-test .
```

## Run tests (mount your code as a volume)

```powershell
# Mount current directory into /app and run pytest
docker run --rm -v "${PWD}:/app" py-iperf3-test
```

The entrypoint script automatically installs the project in editable mode from the mounted volume before running the command.

## Run specific tests or commands

```powershell
# Run a specific test file
docker run --rm -v "${PWD}:/app" py-iperf3-test pytest tests/test_config.py -v

# Run with verbose output
docker run --rm -v "${PWD}:/app" py-iperf3-test pytest -v

# Open a bash shell for debugging
docker run --rm -it -v "${PWD}:/app" py-iperf3-test bash
```

## Run integration tests (need iperf3 server)

```powershell
# Option 1: Start iperf3 server on host, use host networking
# First: iperf3 -s (on host)
docker run --rm --network=host -v "${PWD}:/app" py-iperf3-test pytest -m integration

# Option 2: Run iperf3 server inside the container
docker run --rm -v "${PWD}:/app" py-iperf3-test bash -c "iperf3 -s -D && pytest -m integration"
```

## Environment Variables

- `PY_IPERF3_LIB`: Path to a non-standard libiperf location (e.g., `/usr/local/lib/libiperf.so`)
