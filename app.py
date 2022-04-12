from flask import Flask, request, jsonify
import json
import subprocess
import time
from subprocess import PIPE, run

app = Flask(__name__)

default_json = ["bkup_configs/irvine_2ppe.json","bkup_configs/irvine_bk.json"]

@app.route('/command', methods=['POST'])
def command_recv():
    # ======================================================== #
    # Command Key
    # run: 
    #   type: watchdog / stream
    #   json: name of the json file
    # stop
    # update
    # ======================================================== #
    command = json.loads(request.data)
    print(command)    
    if "run" in command:
        res = run_pipeline(command["run"])
        return "Error" if not res else "Succeed"
    elif "stop" in command:
        stop_pipeline()
    elif "update" in command:
        update_pipeline(command["update"])
    elif "check" in command:
        result = check_pipeline()
        return str(result)
    return '200'

def run_pipeline(command):
    if "type" not in command.keys() or "path" not in command.keys():
        return False
    if "json" not in command.keys():
        for cfg in default_json:
            subprocess.run(["./docker-run.sh",command["type"],cfg,"--opt","background"],cwd=command["path"])
            time.sleep(30)
    else:
        for cfg in command["json"]:
            #print(command["json"])
            subprocess.run(["./docker-run.sh",command["type"],cfg,"--opt","background"],cwd=command["path"])
            time.sleep(30)
    return True

def stop_pipeline():
    # Current design stop all docker image in the server
    # docker stop $(docker ps -q)
    bytes = subprocess.run(["docker","ps","-q"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    str =bytes.stdout.decode("utf-8")
    ids = str.split('\n')[:-1]
    for id in ids:
        subprocess.run(["docker","stop",id])

def update_pipeline(command):
    
    sys_monitor_path = command["sys_monitor_path"]
    eg_pipeline_path = command["eg_pipeline_path"]
    #print(sys_monitor_path,eg_pipeline_path)
    # sys_monitor git pull, git submodule updat, docker build
    subprocess.run(["git","pull"],cwd=sys_monitor_path)
    time.sleep(30)
    subprocess.run(["git","submodule","update","--recursive","--remote"],cwd=sys_monitor_path)
    time.sleep(30)
    subprocess.run(["./docker-build.sh","background"],cwd=sys_monitor_path)
    time.sleep(120)
    # eg_pipeline git pull, git submodule updat, docker build
    subprocess.run(["git","pull"],cwd=eg_pipeline_path)
    time.sleep(30)
    subprocess.run(["git","submodule","update","--recursive","--remote"],cwd=eg_pipeline_path)
    time.sleep(30)
    subprocess.run(["./docker-build.sh"],cwd=eg_pipeline_path)
    time.sleep(120)
    

def check_pipeline():
    bytes = subprocess.run(["docker","ps"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    str =bytes.stdout.decode("utf-8")
    lines = str.split('\n')[1:-1]
    running_image = 0
    for line in lines:
        field = line.split(' ')
        for i in range(len(field)):
            if field[i]== "eg/eg-pipeline":
                running_image += 1
    return running_image


if __name__ == '__main__':
    
    app.run(host='0.0.0.0', port=5502)
