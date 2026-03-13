import os

os.chdir("/home/wtc/docker_upload")

os.system("docker build -t app .")

os.system("docker run -d -p 8080:80 app") # runs in the background 