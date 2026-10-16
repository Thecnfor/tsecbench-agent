# f2-05 靶机文件说明

- `challenge.json` / `Dockerfile` / `app.py` / `web/index.html`：入库
- `web/validator`（ELF 64-bit，md5 `355c98184219abb4891ef4db8a5bd253`）：**不入库**（`.gitignore` 排除）

`web/validator` 缺失时靶机照常启动，只是 `/download` 返回 404。取文件（任一 bench 存档均可）：

```bash
cd exp/tcbench/docker/archives
tar xzf <TOKEN>_<ts>.tar.gz --wildcards '*/tasks/f2-05/validator'
cp <TOKEN>/tasks/f2-05/validator ../../../../arena/challenges/f2-05/web/validator
```
