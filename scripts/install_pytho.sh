#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Convert relative path to absolute path
EMBEDDING_MODELS_DIR="$(realpath "$SCRIPT_DIR/../../embedding-api/models")"
VECTOR_SEARCH_INDEX_DIR="$(realpath "$SCRIPT_DIR/../../vector-search-api/indices")"

# --- Argument parsing --------------------------------------------------
# --host-render / --host-render-only moved to geo-agent/scripts/
# install_geoagent.sh in Task 6. The Playwright driver + host-side
# systemd units now live under the geo-agent repo.
for arg in "$@"; do
    case "$arg" in
        --host-render|--host-render-only)
            echo "ERROR: --host-render moved to geo-agent/scripts/install_geoagent.sh (Task 6)." >&2
            exit 2
            ;;
        --help|-h)
            cat <<EOF
usage: install_pytho.sh

pytho-app installer — Ollama + Docker + Elasticsearch bring-up. The
map render driver (chromium-cdp / cdp-forward systemd units) moved to
geo-agent/scripts/install_geoagent.sh under Task 6 of the map API
unification plan.
EOF
            exit 0
            ;;
    esac
done

# Function to check if a command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to install Ollama on Linux
deploy_linux() {
    if command_exists ollama; then
        echo "Ollama is already installed. Skipping installation."
    else
        echo "Installing Ollama on Linux..."
        curl -fsSL https://ollama.com/install.sh | sh
    fi
}

# Function to install Ollama on macOS
deploy_macos() {
    if command_exists ollama; then
        echo "Ollama is already installed. Skipping installation."
    else
        echo "Installing Ollama on macOS..."
        brew install ollama
    fi
}

# Function to install Ollama on Windows
deploy_windows() {
    if command_exists ollama; then
        echo "Ollama is already installed. Skipping installation."
    else
        echo "Installing Ollama on Windows..."
        powershell -Command "& {Invoke-WebRequest -Uri https://ollama.com/install.exe -OutFile ollama-installer.exe; Start-Process -Wait -FilePath ./ollama-installer.exe; Remove-Item ollama-installer.exe}"
    fi
}

# Function to install Docker
deploy_docker() {
    if command_exists docker; then
        echo "Docker is already installed. Skipping installation."
    else
        echo "Installing Docker..."
        if [[ "$OSTYPE" == "linux-gnu"* ]]; then
            curl -fsSL https://get.docker.com | sh
            sudo usermod -aG docker $USER
            echo "Docker installed. Please restart your session to apply changes."
        elif [[ "$OSTYPE" == "darwin"* ]]; then
            brew install --cask docker
            echo "Please open Docker Desktop manually after installation."
        elif [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
            powershell -Command "& {Invoke-WebRequest -Uri https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe -OutFile docker-installer.exe; Start-Process -Wait -FilePath ./docker-installer.exe; Remove-Item docker-installer.exe}"
            echo "Docker installation complete. Please restart your system."
        else
            echo "Unsupported OS for Docker installation. Please install manually."
        fi
    fi
}

# Detect OS and install Ollama and Docker accordingly
if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    deploy_linux
    deploy_docker
elif [[ "$OSTYPE" == "darwin"* ]]; then
    deploy_macos
    deploy_docker
elif [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
    deploy_windows
    deploy_docker
else
    echo "Unsupported operating system. Please install Ollama and Docker manually."
    exit 1
fi

# Ensure Ollama is in PATH
export PATH="$HOME/.ollama/bin:$PATH"

# Pull and launch a model
MODEL_NAME="falcon3:10b"
echo "Downloading and launching $MODEL_NAME..."
ollama pull "$MODEL_NAME"

# Run another script at the end
SECOND_SCRIPT="$SCRIPT_DIR/../../vector-db/load.sh"
if [ -f "$SECOND_SCRIPT" ]; then
    echo "Giving execution permission to post-install script: $SECOND_SCRIPT"
    chmod +x "$SECOND_SCRIPT"
    echo "Running post-install script: $SECOND_SCRIPT"
    bash "$SECOND_SCRIPT"
else
    echo "Post-install script not found: $SECOND_SCRIPT"
fi

docker rm proxy-app --force
docker rm docker-api --force

docker rmi pytho-app-docker-api pytho-app-nginx

ELASTIC_PASSWORD=<REDACTED>
ES_DATA_DIR="${PYTHO_DATA:-$SCRIPT_DIR/../data}/es-data"
mkdir -p "$ES_DATA_DIR"
chown 1000:1000 "$ES_DATA_DIR"
docker run -d --name es-secure \
  --restart on-failure:5 \
  --security-opt no-new-privileges:true \
  --pids-limit 300 \
  --memory 1g \
  --cpus 2.0 \
  --health-cmd="curl -sf -u elastic:$ELASTIC_PASSWORD http://127.0.0.1:9200/_cluster/health || exit 1" \
  --health-interval=30s \
  --health-timeout=5s \
  --health-start-period=30s \
  --health-retries=3 \
  --log-driver json-file \
  --log-opt max-size=10m \
  --log-opt max-file=5 \
  -v "$ES_DATA_DIR":/usr/share/elasticsearch/data \
  -p 127.0.0.1:9200:9200 -p 127.0.0.1:9300:9300 \
  -e "discovery.type=single-node" \
  -e "xpack.security.enabled=true" \
  -e "xpack.security.http.ssl.enabled=false" \
  -e "ELASTIC_PASSWORD=$ELASTIC_PASSWORD" \
  -e "ES_JAVA_OPTS=-Xms512m -Xmx512m" \
  docker.elastic.co/elasticsearch/elasticsearch:9.1.4

curl -s -u elastic:$ELASTIC_PASSWORD \
  -H 'Content-Type: application/json' \
  -X POST 'http://localhost:9200/_security/api_key' \
  -d '{
    "name": "doc-chunks-service",
    "role_descriptors": {
      "doc_chunks_rw": {
        "cluster": [],
        "indices": [
          {
            "names": ["doc_chunks", "doctrine_chunks"],
            "privileges": [
              "read",
              "write",
              "delete",
              "create_index",
              "view_index_metadata"
            ]
          }
        ]
      }
    }
  }'

(cd "$SCRIPT_DIR/../" && docker compose -f docker-compose-edge.yml up -d)

