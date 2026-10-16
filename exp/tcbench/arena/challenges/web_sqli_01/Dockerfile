# 示例靶机：stdlib-only（无 pip 装依赖，构建离线可完成）。
# flag 不烤进镜像——arena 在 docker run 时以 FLAG_0 环境变量注入（flag_inject: env）。
FROM python:3.12-alpine

WORKDIR /app
COPY app.py /app/app.py

EXPOSE 8080
CMD ["python", "app.py"]
