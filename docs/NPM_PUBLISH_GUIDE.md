# NPM 发布指南（lark-remote-claude）

> 目标：用 **owner 账号**快速发布新版本到 npm。

## 一次发布的最短流程

```bash
# 1) 确认版本号已更新（例如 1.0.2 -> 1.0.3）
# 编辑 package.json 的 version

# 2) 使用 npm 官方 registry 登录 owner 账号（推荐 web 登录）
npm login --registry=https://registry.npmjs.org/ --auth-type=web
npm whoami --registry=https://registry.npmjs.org/   # 必须是 shelly_0809

# 3) 发布
npm publish --access public

# 4) 验证
npm view lark-remote-claude version
```

## 发布前检查（建议）

```bash
# 查看当前改动
git status --short

# 确认当前 registry（应为 npm 官方）
npm config get registry

# 预览打包内容（不真正发布）
npm pack --dry-run
```

> 如果你平时使用公司镜像源（例如 bnpm），建议登录和发布时显式带上 `--registry=https://registry.npmjs.org/`，避免误发到非官方源。

## 常见问题

### 1) `npm login` 没走网页，要求输入用户名/密码
通常是因为当前 npm registry 不是官方源（例如 `https://bnpm.byted.org/`）。

建议用官方源并强制 web 登录：

```bash
npm login --registry=https://registry.npmjs.org/ --auth-type=web
npm whoami --registry=https://registry.npmjs.org/
```

### 2) `E404 Not Found - PUT https://registry.npmjs.org/lark-remote-claude`
通常是**当前账号没有该包发布权限**（即使包存在也会报 404）。

排查：

```bash
npm whoami
npm owner ls lark-remote-claude
```

处理方式（二选一）：

- 方式 A（最快）：切到 owner 账号发布
  ```bash
  npm login
  npm whoami   # shelly_0809
  npm publish --access public
  ```

- 方式 B：给当前账号加 owner
  ```bash
  npm owner add <your-npm-id> lark-remote-claude
  ```

### 3) 版本号已存在，发布失败
npm 不允许重复发布同版本，请先升级 `package.json.version` 后再发布。

### 4) 发布后本地安装还是旧版本
确认安装源和缓存：

```bash
npm view lark-remote-claude version
npm i -g lark-remote-claude@latest
```

## 推荐发布习惯

- 每次发布都先 `npm whoami`，确认账号身份。
- 先 `npm pack --dry-run` 检查包内容，避免漏文件或打入无关文件。
- 发布后立刻用 `npm view lark-remote-claude version` 验证线上版本。
