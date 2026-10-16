# f2-05 靶机：静态页 + /download 下发 validator 二进制（stdlib only，无 pip 依赖）。
# flag 是逆向产物（flag_inject: none）——不注入容器、不在镜像里、不在网页里。
FROM python:3.12-alpine

WORKDIR /app
COPY app.py /app/app.py
COPY web /app/web

EXPOSE 8080
CMD ["python", "app.py"]
