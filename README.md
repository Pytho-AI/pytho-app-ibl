## FastAPI + React + Docker + Nginx

### Dependencies
1. Ollama
2. Docker
3. OnlyOffice `docker run -i -t -d -p 81:80 --restart=always -e JWT_ENABLED=false onlyoffice/documentserver`
### Getting Started
1. Clone the repo and cd the directory.
2. Set up the virtual env for python backend - `virtualenv virtual`
3. Activate virtualenv `. virtual/bin/activate` and install dependencies - `pip install -r app/requirements.txt`. Once installed, you can deactivate virtualenv.
4. Install ui dependencies - `yarn --cwd ./ui `. `--cwd ./ui` allows to specify directory where package.json is located.
5. Setup Elasticsearch Docker:

   **Important:** Set a password and ensure ports are properly exposed with `-p 9200:9200 -p 9300:9300`
   
   ```bash
   # Set your Elasticsearch password
   ELASTIC_PASSWORD=some_password
   
   # Run Elasticsearch container with proper port mapping
   docker run -d --name es-prod \
     --restart unless-stopped \
     -p 9200:9200 -p 9300:9300 \
     -e "discovery.type=single-node" \
     -e "xpack.security.enabled=true" \
     -e "xpack.security.http.ssl.enabled=false" \
     -e "xpack.monitoring.collection.enabled=true" \
     -e "logger.level=ERROR" \
     -e "ELASTIC_PASSWORD=${ELASTIC_PASSWORD}" \
     -e "ES_JAVA_OPTS=-Xms2g -Xmx2g" \
     -v es_data:/usr/share/elasticsearch/data \
     -v es_logs:/usr/share/elasticsearch/logs \
     docker.elastic.co/elasticsearch/elasticsearch:9.1.4
   ```
   
   **Verify Elasticsearch is running:**
   ```bash
   # Wait ~15 seconds for ES to start, then test:
   curl -u elastic:pytho http://localhost:9200
   
   # You should see ES version info. If you get "Connection refused", 
   # check that ports are exposed: docker ps | grep elastic
   # Should show: 0.0.0.0:9200->9200/tcp
   ```

6. Create an API key for Elasticsearch:

   ```bash
   # Use the same password you set above
   curl -s -u elastic:pytho \
     -H 'Content-Type: application/json' \
     -X POST 'http://localhost:9200/_security/api_key' \
     -d '{
       "name": "doc-chunks-service",
       "role_descriptors": {
         "doc_chunks_rw": {
           "cluster": [],
           "indices": [
             {
               "names": ["doc_chunks"],
               "privileges": [
                 "read",
                 "write",
                 "delete",
                 "create_index",
                 "manage",
                 "view_index_metadata"
               ]
             }
           ]
         }
       }
     }'
   ```

   **You should see output like this:**
   ```json
   {"id":"<REDACTED>","name":"doc-chunks-service","api_key":"<REDACTED>","encoded":"<REDACTED>"}
   ```

7. Update your environment files with the API credentials:
   
   Copy the `id` and `api_key` values from the response into both:
   - `app/.env.local` (for local development)
   - `app/.env` (for Docker deployment)
   
   ```bash
   ES_ACCESS_ID="fTY2RJwBOxb3hZWZwXFE"
   ES_ACCESS_KEY="n86mOcKckB6GG9Vn8JJa5A"
   ```

8. **Troubleshooting:**
   - **401 Authentication error:** Your API key is incorrect or expired. Regenerate using step 6.
   - **Connection refused:** Elasticsearch isn't running or ports aren't exposed. Check with `docker ps` and recreate container if needed.
   - **Can't connect after restart:** Make sure to restart your backend (`./develop.sh`) after updating `.env.local`
9. Launch the audio generation docker:
```bash
docker run -d --restart=unless-stopped -p 8880:8880 ghcr.io/remsky/kokoro-fastapi-cpu:latest
```

10. You are all set up! Now run `./develop.sh` and you have a live development environment. Make changes to app or ui code and it will hot-reload.

### Installing Containers
`chmod +x install_pytho.sh`
`./install_pytho.sh docker`

### Running locally on macOS (one-shot setup)

To get the UI and API running on your Mac for local testing (Docker must be installed and running):

```bash
# From the pytho-app directory:
chmod +x scripts/install-mac.sh
ELASTIC_PASSWORD=yourpassword ./scripts/install-mac.sh
# or: ./scripts/install-mac.sh yourpassword
```

The script will: ensure Docker is running, start Elasticsearch, create an ES API key, write `app/.env` and `ui/.env`, then run `docker compose up` for the API and UI.

- **UI:** http://localhost:8080  
- **API:** http://localhost:8000  

Optional: set `PYTHO_DATA` to use a different data directory (default: `./data`).

If you already have Elasticsearch running and `app/.env` configured, you can start just the app and UI:

```bash
export ELASTIC_PASSWORD=yourpassword
export PYTHO_DATA=./data   # or your data path
# Ensure app/.env and ui/.env exist (ui/.env can be empty)
docker compose up -d --build docker-api nginx
```
