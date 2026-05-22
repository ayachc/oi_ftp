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

默认端口是 `5000`。启动时如果没有手动指定端口，服务会优先读取 `ftp_data/config.json` 里的 `port`，如果配置里也没有，就使用 `5000` 并写回配置文件。启动后终端会显示本机访问地址和局域网访问地址，其他用户在同一局域网中打开 `http://本机IP:端口` 即可。

如果临时想覆盖配置文件里的端口，可以手动指定：

```bash
python server.py --port 8080
```

打包后的可执行文件也支持同样参数：

```bash
./oi-ftp --port 8080
```

## 数据目录

服务首次启动会创建：

- `ftp_data/download`：管理员发布的下载文件。
- `ftp_data/upload`：用户提交的文件和文件夹。
- `ftp_data/config.json`：管理员密码和上传大小限制。
- `ftp_data/metadata.json`：上传者和时间记录。

默认管理员密码是 `admin123`，可在 `ftp_data/config.json` 修改。

`ftp_data/config.json` 示例：

```json
{
  "admin_password": "admin123",
  "file_size_limit_mb": 5,
  "port": 5000,
  "local_ips": [
    "192.168.1.8",
    "10.0.0.5"
  ]
}
```

其中 `local_ips` 会在服务启动时自动检测并写入。如果电脑同时连接多个局域网，检测到的地址会全部列出。

## 打包为单文件

在有 Python 的开发机上安装 PyInstaller 后执行：

```bash
python -m pip install pyinstaller
python build.py
```

生成的 `dist/oi-ftp` 或 `dist/oi-ftp.exe` 可复制到老师电脑上双击运行，不需要目标电脑安装 Python。

注意：PyInstaller 只能打包当前系统平台的可执行文件。要发布 Windows 和 Linux 两个平台，建议分别在 Windows 和 Linux 上执行 `python build.py`。

## 发布到 GitHub Release

推荐使用 GitHub CLI：

```bash
git status
git add .
git commit -m "Release v1.0.0"
git tag v1.0.0
git push origin main
git push origin v1.0.0
gh release create v1.0.0 dist/oi-ftp* --title "v1.0.0" --notes "首次发布文件收发服务"
```

如果不用 GitHub CLI，也可以在仓库页面打开 `Releases`，点击 `Draft a new release`，选择或创建 `v1.0.0` 标签，上传 `dist` 里的可执行文件后发布。
