wsl -d Ubuntu -- bash -lc '
sudo apt-get update -y >/dev/null
sudo apt-get install -y git >/dev/null

cd /tmp
sudo rm -rf docker-bench-security
git clone --depth 1 https://github.com/docker/docker-bench-security.git >/dev/null
cd docker-bench-security

sudo ./docker-bench-security.sh
'
