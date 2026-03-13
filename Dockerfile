FROM nginx:alpine
#reference my html 
COPY index.html /usr/share/nginx/html/
#reference my json 
COPY questions.json /usr/share/nginx/html/
COPY style.css /usr/share/nginx/html/
