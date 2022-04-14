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
        stop_pipeline(command["stop"])
    elif "update" in command:
        update_pipeline(command["update"])
    elif "check" in command:
        print("Get command Check")
        result = check_pipeline(command["check"])
        return str(result)
    return '200'

def run_pipeline(cfg):
    if "type" not in cfg.keys() or "json" not in cfg.keys() or "path" not in cfg.keys():
        return False
    for json in cfg["json"]:
        print("should run",json," with ",cfg["type"]," at path ",cfg["path"])
        subprocess.run(["./docker-run.sh",cfg["type"],json,"--opt","background"],cwd=cfg["path"])
        time.sleep(30)
    return True

def stop_pipeline(image_name):
    subprocess.run(["docker","stop",image_name])

def update_pipeline(command):
    
    sys_monitor_path = command["sys_monitor_path"]
    eg_pipeline_path = command["eg_pipeline_path"]
    #print(sys_monitor_path,eg_pipeline_path)
    # sys_monitor git pull, git submodule updat, docker build
    subprocess.run(["git","pull"],cwd=sys_monitor_path)
    time.sleep(30)
    subprocess.run(["git","submodule","update","--recursive","--remote"],cwd=sys_monitor_path)
    time.sleep(30)
    subprocess.run(["./docker-build.sh","--opt","background"],cwd=sys_monitor_path)
    time.sleep(120)
    # eg_pipeline git pull, git submodule updat, docker build
    subprocess.run(["git","pull"],cwd=eg_pipeline_path)
    time.sleep(30)
    subprocess.run(["git","submodule","update","--recursive","--remote"],cwd=eg_pipeline_path)
    time.sleep(30)
    subprocess.run(["./docker-build.sh"],cwd=eg_pipeline_path)
    time.sleep(120)
    

def check_pipeline(image_name):
    target = image_name
    bytes = subprocess.run(["docker","ps"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    str =bytes.stdout.decode("utf-8")
    lines = str.split('\n')[1:-1]
    all_running_image = []
    for i in range(len(lines)):
        all_running_image.append(lines[i].split(' ')[-1])
    if target in all_running_image:
        print("target image is running")
        return True
    else:
        return False
    

if __name__ == '__main__':
    
    app.run(host='0.0.0.0', port=5502)
