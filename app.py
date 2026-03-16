import os

os.chdir("/home/wtc/docker_upload")

# 1. Build for the correct Cloud architecture (AMD64)
# We also tag it with the Google Registry path immediately
IMAGE_PATH = "europe-west1-docker.pkg.dev/tidal-glider-488520-a8/cloud-run-source-deploy/quiz-website:latest"
os.system(f"docker build --platform linux/amd64 -t {IMAGE_PATH} .")

# 2. Push the image to Google Artifact Registry
# (Make sure you've run 'gcloud auth configure-docker europe-west1-docker.pkg.dev' once in terminal)
os.system(f"docker push {IMAGE_PATH}")

# 3. Deploy/Update the existing Cloud Run server
# We include '--port 80' to fix the Nginx 8080 error without changing your Dockerfile
SERVICE_NAME = "quiz-website" # Change this to your actual service name
os.system(f"gcloud run deploy {SERVICE_NAME} --image {IMAGE_PATH} --region europe-west1 --port 80 --allow-unauthenticated --project tidal-glider-488520-a8")
	
