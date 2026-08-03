@ECHO OFF
docker run -it -v %cd%\data:/data:rw -v %cd%\conf:/conf:ro -v %cd%\results:/results:rw ef5-container
pause