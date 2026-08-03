#!/usr/bin/env bash
# init
function pause(){
   read -p "$*"
}

docker run -it -v $(pwd)/data:/data:rw -v $(pwd)/conf:/conf:ro -v $(pwd)/results:/results:rw -u `id -u $USER`:`id -g $USER` ef5-container
pause 'Press any key to continue...'
