# Aria2 Blackhole

A Python script that watches a directory for new torrent files, submits them to the [Aria2](https://github.com/aria2/aria2/) download queue and moves the downloaded files to a destination directory.

## Setup & Usage

~~~ bash
docker run --name aria2 -d --rm -v aria2-downloads:/app/data --network host ghcr.io/dadevel/aria2
docker run --name aria2-blackhole -d --rm -e ARIA2_BLACKHOLE_API_TOKEN=changeme -v $PWD/torrents:/app/data/torrents -v aria2-downloads:/app/data/downloads -v $PWD/downloaded:/app/data/files --network host ghcr.io/dadevel/aria2-blackhole
# append a torrent to the aria2 download queue
curl -o ./torrents/ubuntu.torrent https://releases.ubuntu.com/20.04/ubuntu-20.04.3-live-server-amd64.iso.torrent
# wait for the completed download to appear
watch -n 3 ls -l ./downloaded
~~~

## Configuration

Environment variable | Description | Default
---------------------|-------------|--------
`ARIA2_BLACKHOLE_ENDPOINT` | Aria2 WebSocket URL | `ws://localhost:6800/jsonrpc`
`ARIA2_BLACKHOLE_TOKEN` | Aria2 RPC password | none
`ARIA2_BLACKHOLE_TORRENT_DIR` | directory to watch for torrent files | `./torrents`
`ARIA2_BLACKHOLE_DOWNLOAD_DIR` | directory where Aria2 stores its downloads | `./downloads`
`ARIA2_BLACKHOLE_STORAGE_DIR` | directory to move completed downloads to | `./files`
`ARIA2_BLACKHOLE_STATE_FILE` | file to persist internal state between restarts | `./downloads/state.json`
`ARIA2_BLACKHOLE_LOG_LEVEL` | one of the log levels from [Pythons logging module](https://docs.python.org/3/library/logging.html#logging-levels) | `info`

## Development

Install the dependencies and run the Python script.

~~~ bash
pip3 install -r ./requirements.txt
python3 ./main.py
~~~

The `Dockerfile` is available under [github.com/dadevel/dockerfiles](https://github.com/dadevel/dockerfiles/tree/main/aria2-blackhole).
