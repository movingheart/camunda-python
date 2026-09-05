#!/bin/bash
# 推送脚本：创建 GitHub camunda-python 仓库 + push main 分支
# 执行方式：直接 bash run（无参数）
# 依赖：环境变量 $GH_TOKEN 由调用方临时注入

set -euo pipefail

OWNER="movingheart"               # GitHub 用户名（从 /user API 拿到的 login）
REPO="camunda-python"
DESCRIPTION="Python 3 BPMN/DMN engine semantically aligned with Camunda 7 (Apache-2.0 independent implementation, M0~M9)"
HOMEPAGE=""   # 不填
PRIVATE=false
BRANCH="main"

if [[ -z "${GH_TOKEN:-}" ]]; then
  echo "ERROR: GH_TOKEN 未设置。请把 PAT 贴出来，我用 \$GH_TOKEN 临时注入。" >&2
  exit 1
fi

cd "$(dirname "$0")"   # 切到项目根（脚本应在 camunda/ 下）

echo "==> 1. 检查 token 有效性"
USER_LOGIN=$(curl -fsS -H "Authorization: token $GH_TOKEN" \
                  -H "Accept: application/vnd.github+json" \
                  https://api.github.com/user | python3 -c "import sys,json; print(json.load(sys.stdin)['login'])")
echo "    登录用户: $USER_LOGIN"
if [[ "$USER_LOGIN" != "$OWNER" ]]; then
  echo "WARN: 期望 owner=$OWNER，实际登录=$USER_LOGIN（不影响创建）"
fi

echo "==> 2. 检查仓库是否已存在"
HTTP_CODE=$(curl -s -o /tmp/repo_resp.json -w "%{http_code}" \
                -H "Authorization: token $GH_TOKEN" \
                -H "Accept: application/vnd.github+json" \
                "https://api.github.com/repos/$OWNER/$REPO")
if [[ "$HTTP_CODE" == "200" ]]; then
  echo "    仓库已存在（HTTP 200），跳过创建"
elif [[ "$HTTP_CODE" == "404" ]]; then
  echo "==> 3. 创建仓库 $OWNER/$REPO"
  curl -fsS -X POST \
       -H "Authorization: token $GH_TOKEN" \
       -H "Accept: application/vnd.github+json" \
       "https://api.github.com/user/repos" \
       -d "{\"name\":\"$REPO\",\"description\":\"$DESCRIPTION\",\"private\":$PRIVATE}" \
       > /tmp/repo_create.json
  echo "    仓库创建成功"
else
  echo "ERROR: 仓库查询失败 HTTP=$HTTP_CODE" >&2
  cat /tmp/repo_resp.json >&2
  exit 2
fi

echo "==> 4. 调整 git remote（移除 GitLab，加 GitHub）"
git remote remove origin 2>/dev/null || true
git remote add origin "https://$OWNER:$GH_TOKEN@github.com/$OWNER/$REPO.git"
git remote -v

echo "==> 5. push main 分支"
git push -u origin main 2>&1 | tail -20

echo "==> 6. push tags（如有）"
git push --tags origin 2>&1 | tail -5 || true

echo ""
echo "=========================================="
echo "完成。仓库地址："
echo "  https://github.com/$OWNER/$REPO"
echo "=========================================="

echo ""
echo "==> 7. 清理：把 remote URL 中的 token 抹掉"
git remote set-url origin "https://github.com/$OWNER/$REPO.git"
echo "    已清理 remote URL（移除 embedded token）"

echo "==> 8. 清理临时文件"
rm -f /tmp/repo_resp.json /tmp/repo_create.json

echo ""
echo "推送完成 ✅"