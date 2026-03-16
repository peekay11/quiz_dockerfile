import os

os.chdir("/home/wtc/docker_upload")

# 1. Update Path to africa-south1
# Note the change in the URL and the region flag
IMAGE_PATH = "africa-south1-docker.pkg.dev/tidal-glider-488520-a8/cloud-run-source-deploy/quiz-website:latest"

# 2. Build for AMD64 (Sudo required for WTC machines)
os.system(f"sudo docker build --platform linux/amd64 -t {IMAGE_PATH} .")

# 3. Push to the new SA Registry
os.system(f"sudo docker push {IMAGE_PATH}")

# 4. Deploy to africa-south1
SERVICE_NAME = "quiz-website"
os.system(f"gcloud run deploy {SERVICE_NAME} --image {IMAGE_PATH} --region africa-south1 --port 80 --allow-unauthenticated --project tidal-glider-488520-a8")