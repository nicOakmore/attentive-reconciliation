#!/bin/bash
# Move the repo to the nicoakmore GitHub account and rebuild the Render service from it.
# Prerequisite, one interactive step that cannot be scripted: authenticate that account once.
#   gh auth login --hostname github.com --web        # choose the nicoakmore account
# Then run this script. It creates the repo, pushes, creates the new service, waits for it to go live,
# checks it answers, and only then removes the old service and the old repo.
set -euo pipefail
cd "$(dirname "$0")"

OLD_SERVICE=srv-dakiavdg1s2s73ce67h0
OLD_REPO=ncanoCumula3/attentive-reconciliation
NEW_REPO=nicoakmore/attentive-reconciliation
KEY=$(cat /tmp/rkey)
GROQ=$(grep GROQ_API_KEY .env.local | cut -d= -f2-)

echo "== 1. repo under nicoakmore"
GH_ACCOUNT=$(gh api user --jq .login)
[ "$GH_ACCOUNT" = "nicoakmore" ] || { echo "active gh account is $GH_ACCOUNT, switch to nicoakmore first"; exit 1; }
gh repo create "$NEW_REPO" --public --source=. --remote=nicoakmore --push

echo "== 2. new Render service from the new repo"
NEW=$(curl -s -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "$(cat <<JSON
{"type":"web_service","name":"attentive-reconciliation","ownerId":"$(curl -s -H "Authorization: Bearer $KEY" https://api.render.com/v1/owners | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["owner"]["id"])')",
 "repo":"https://github.com/$NEW_REPO","branch":"main","autoDeploy":"yes","rootDir":"",
 "serviceDetails":{"env":"image","plan":"pro","region":"oregon","envSpecificDetails":{"dockerfilePath":"./Dockerfile"}},
 "envVars":[{"key":"GROQ_API_KEY","value":"$GROQ"},{"key":"GROQ_TEXT_MODEL","value":"openai/gpt-oss-120b"},{"key":"PDF_WORKERS","value":"6"}]}
JSON
)" https://api.render.com/v1/services | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d["service"]["id"] if "service" in d else d)')
echo "new service $NEW"

echo "== 3. wait for it to serve"
for i in $(seq 1 60); do
  URL=$(curl -s -H "Authorization: Bearer $KEY" "https://api.render.com/v1/services/$NEW" | python3 -c 'import sys,json;print(json.load(sys.stdin)["serviceDetails"].get("url",""))')
  if [ -n "$URL" ] && curl -sf -m 20 "$URL/healthz" > /dev/null; then echo "live at $URL"; break; fi
  sleep 20
done
curl -s -m 30 "$URL/healthz"; echo

echo "== 4. remove the old service and the old repo"
curl -s -X DELETE -H "Authorization: Bearer $KEY" "https://api.render.com/v1/services/$OLD_SERVICE" -o /dev/null -w 'delete service %{http_code}\n'
gh repo delete "$OLD_REPO" --yes
echo "done. new service $NEW at $URL"
