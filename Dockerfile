FROM python:3.9-slim

# Set the working directory to /app
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# move config file to config directory
COPY fritzinfluxdb.ini-sample /app/config
RUN rm /app/fritzinfluxdb.ini-sample

# Install any needed packages specified in requirements.txt
RUN pip install --trusted-host pypi.python.org -r requirements.txt

# run daemon
CMD [ "python", "./fritzinfluxdb.py" ]

# EOF
