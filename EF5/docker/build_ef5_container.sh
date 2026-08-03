#!/usr/bin/env bash
# init
function pause(){
   read -p "$*"
}

docker build --no-cache -t ef5-container .
pause 'Press any key to continue...'