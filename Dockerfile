FROM python:3.9-slim

# Make the /app dir
RUN mkdir /app

# Set the working directory to /app
WORKDIR /app

# Copy required files to /app
COPY fritzinfluxdb.py requirements.txt ./

# move config file to config directory
COPY fritzinfluxdb.ini-sample /app/config
RUN rm /app/fritzinfluxdb.ini-sample

# Install any needed packages specified in requirements.txt
RUN pip install --trusted-host pypi.python.org -r requirements.txt

RUN mkdir config

# run daemon
CMD [ "python", "./fritzinfluxdb.py" ]

# EOF
