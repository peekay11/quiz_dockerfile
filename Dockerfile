FROM nginx:alpine

COPY index.html    /usr/share/nginx/html/
COPY style.css     /usr/share/nginx/html/
COPY database.py   /usr/share/nginx/html/
COPY questions.json /usr/share/nginx/html/