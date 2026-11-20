# OCI Image Unpacker

离线排查容器文件缺失时，从交付镜像还原最终根文件系统的服务；同时可把多平台
交付镜像裁剪成单平台、随包保留构建证明的 OCI Image Layout tar，供隔离站点使用。

- `POST /unpack`：上传仅含单个 manifest 的 OCI Image Layout tar，返回还原后的 `rootfs.tar`；
- `POST /export`：上传未压缩的多平台 OCI Image Layout tar，提交目标平台，返回裁剪后的 OCI tar。

## 快速开始（docker compose）

```bash
docker compose up --build
```

服务监听 <http://localhost:8000>，打开 <http://localhost:8000/docs>：

1. 先调 `GET /sample-image` 下载样例镜像 `sample-image.tar`（Swagger 里点 "Download file"）；
2. 再在 `POST /unpack` 里上传该 tar，响应即为 `rootfs.tar`。

命令行等价操作：

```bash
curl -OJ http://localhost:8000/sample-image          # 下载样例镜像
curl -F "file=@sample-image.tar" -OJ \
     http://localhost:8000/unpack                    # 得到 rootfs.tar
tar -tf rootfs.tar                                   # 查看还原结果

# 裁剪多平台镜像：注意上传的是未压缩的 OCI Image Layout tar
curl -F "file=@multi-platform.tar" \
     -F "os=linux" -F "architecture=arm64" -F "variant=v8" \
     -OJ http://localhost:8000/export                 # 得到 linux-arm64-v8.tar
```

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest                 # 运行测试
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/unpack` | 上传 OCI Image Layout tar（单 manifest），返回 `rootfs.tar`；任何校验/解包失败返回 400，不返回残缺文件系统 |
| `POST` | `/export` | 上传多平台 OCI Image Layout tar，表单提交 `os`、`architecture` 与可选 `variant`，返回裁剪后的 OCI tar；目标缺失、匹配不唯一或保留 blob 校验失败返回 400 |
| `GET` | `/sample-image` | 下载样例镜像（含删除、opaque、硬链接覆盖） |
| `GET` | `/healthz` | 健康检查 |

## 校验与还原语义

- **完整性校验**：index.json 引用的 manifest blob、manifest 引用的 config 与
  各 layer blob 都会核对**存在性、字节大小、SHA-256**；每个 layer 解压后
  （gzip 层先解压）再与 config 中 `rootfs.diff_ids` 逐一核对。层数与
  diff_ids 数量不一致、摘要不符、缺块、损坏，整次请求报错。
- **层格式**：支持未压缩层（`...layer.v1.tar`）与 gzip 层
  （`...layer.v1.tar+gzip`，含 docker 对应 mediaType）。
- **按清单层序还原**：普通文件、目录、符号链接、硬链接与权限位（含
  setuid/sgid/sticky）；目录权限与 mtime 在全部层应用完后统一设置，避免
  只读目录阻断同层/上层写入。
- **whiteout / opaque**：每层先扫描全部条目，把 `.wh.<name>` 与
  `.wh..wh..opq` 的删除动作**只作用于下层文件系统**，然后再写入本层新
  内容。因此删除标记在层内的位置（文件前/后）不会影响同层新增文件。
- **硬链接覆盖安全**：新文件先写临时文件再 `rename` 替换，绝不原地截断，
  所以覆盖硬链接的一端时，另一端保留原字节。输出 tar 中仍共享 inode 的
  硬链接会以硬链接条目保留。
- **路径安全**：条目路径拒绝绝对路径与 `..`；符号链接目标（绝对目标按
  rootfs 内解释）与硬链接目标经解析后必须落在隔离目录内，写入前再随
  符号链接逐层解析校验，任何逃逸都会让整次请求失败。
- **请求隔离**：每个请求使用独立临时目录，失败即清理，成功响应发送后
  清理；请求之间互不影响。

## 平台裁剪导出（`POST /export`）

上传**未压缩**的 OCI Image Layout tar（可含多个平台与 BuildKit 证明），
通过表单字段指定目标：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `os` | 是 | 目标操作系统，如 `linux` |
| `architecture` | 是 | 目标架构，如 `amd64`、`arm64` |
| `variant` | 否 | 目标变体，如 `v8`；不填则只匹配声明了**无** variant 的条目 |

image config 中的 `os`、`architecture`、`variant` 精确比较；docker 媒体类型
的清单若 config 缺少平台字段，回退到 index 描述符中的 `platform`。匹配不到
或匹配到多个镜像时只返回错误。`index.json` 既可直接列出镜像清单，也可只引用
**一个**子索引（该子索引再列清单）；两层索引都支持 docker / OCI 媒体类型。

导出结果：

- `index.json` 重写为只列被选中的镜像清单描述符（描述符按保留字节重建）；
  `oci-layout` 原样保留；
- 只保留被选中镜像的 manifest、config、各 layer，以及 `annotations` 中
  `vnd.docker.reference.digest` 指向**该镜像清单**的 BuildKit 证明
  （证明自身的 manifest / config / layer 一并保留）；
- 保留的清单与所引 blob **沿用原字节**，layer 不展开、不改写；被多个描述符
  共用的 blob 只写一份；裁掉的 blob 既不读取完整内容也不写入；
- 每个保留 blob 复制时按块（1 MiB）读取并核对**字节大小与 SHA-256**，任何
  不符即整次请求失败，不返回残缺 tar；
- 未选中平台独有的 blob 即使整体缺失，导出仍可成功（这些平台不会被读取）；
- 请求使用独立临时目录，输出先写临时文件再原子替换，失败即清理。

## 样例镜像内容

两层（第一层 gzip，第二层未压缩），用于验证上述语义：

- 下层文件 `app/config.txt`、`app/data/remove.txt` 被 whiteout 删除；
- `secret/` 被标记 opaque：下层的 `secret/token.txt` 消失，同层新增的
  `secret/public.txt` 保留；
- `app/data/rebuilt.txt` 在同层先写新内容、后出现其 whiteout —— 新内容
  必须保留；
- `bin/tool` 与 `bin/tool-link` 在下层是同一硬链接，上层只覆盖
  `bin/tool`（`tool-v2`），`bin/tool-link` 必须仍是 `tool-v1`；
- 相对符号链接 `bin/tool-alias -> tool` 与绝对符号链接
  `etc/motd-link -> /etc/motd`；
- 权限位：`bin/tool` 0755、`secret/` 0700。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `UNPACK_WORKDIR` | `$TMPDIR/oci-unpacker` | 请求临时目录与样例镜像存放位置 |
