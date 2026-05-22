# 文件收发 Web 服务

轻量、免依赖源码版的局域网文件收发服务，适合同一局域网中的文件分发与收集。

## 启动

Linux/macOS：

```bash
./run.sh
```

Windows：

```bat
双击 run.bat
```

默认端口是 `8080`。启动后终端会显示本机访问地址和局域网访问地址，其他用户在同一局域网中打开 `http://本机IP:8080` 即可。

## 数据目录

服务首次启动会创建：

- `ftp_data/download`：管理员发布的下载文件。
- `ftp_data/upload`：用户提交的文件和文件夹。
- `ftp_data/config.json`：管理员密码和上传大小限制。
- `ftp_data/metadata.json`：上传者和时间记录。

默认管理员密码是 `admin123`，可在 `ftp_data/config.json` 修改。

## 打包为单文件

在有 Python 的开发机上安装 PyInstaller 后执行：

```bash
python -m pip install pyinstaller
python build.py
```

生成的 `dist/oi-ftp` 或 `dist/oi-ftp.exe` 可复制到老师电脑上双击运行，不需要目标电脑安装 Python。
