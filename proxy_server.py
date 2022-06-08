from crypt import methods
import json
from tkinter.messagebox import NO
from flask import Flask,  request, render_template, Response
from os.path import join, realpath, dirname
import asyncio
import httpx

app = Flask(__name__)

app._static_folder = join(realpath(dirname(__file__)), 'templates/static')

img_n_status= {}

@app.route('/')
@app.route('/index', methods=['GET'])
def index():
    ''' Method to render the index.html with the dict of name and status of docker container.'''

    return render_template('index.html', data=img_n_status)


@app.route('/manage_docker', methods=['GET', 'POST'])
async def manage_docker():
    '''Endpoint that catch request from client to work with command and targets.
        
        Example:
            {
                "command": 'stream' | 'watchdog' | 'stop' | 'update'
                "lst_image": 'SBC-cam6-8' | 'SBC-cam9-16' | 'SBC-cam9-18'
            }
    '''

    req_json = request.get_json()
    docker_command = req_json['command']
    docker_images = req_json['lst_images']
    print('Client res', docker_command, docker_images)

    if (docker_command in ["watchdog", "stream", "update"]):
        await run_n_update(docker_command, docker_images)
    elif (docker_command == "stop"):
        await stop_container(docker_command, docker_images)
    else:
        return Response("Command not found", 400)

    return Response('Manage docker', 200)

@app.route('/get_status', methods=['GET'])
async def get_status(command='check'):
    ''' Endpoint that send the status of docker_container 
        
        Example:

            data = {
                "check": "SBC-cam9-14"
            }

            url = 'http://10.230.1.205:5502/command'

            img_n_status {
                "SBC-cam9-14": "True",
                "SBC-cam6-8": "False"
            }
    '''

    tasks_stop = []
    for image_name in cfg.keys():
        image_cfg = cfg[image_name]
        data = json.dumps({command:image_cfg["image_name"]})    
        url = 'http://' + image_cfg["ip"] + ':' + image_cfg["port"] + '/command'
        print("Request send to:", image_cfg["ip"])
        print("Target Image: ", image_name)
        print("Command:", command)
        print("Data: ",data)
        tasks_stop.append(asyncio.create_task(send_command(url, data)))

    res = await asyncio.gather(*tasks_stop)
    img_n_status = {image_name : status for status, image_name in zip(res, cfg.keys())}

    return img_n_status

async def stop_container(command, docker_images):
    ''' Method to formulate json and url to stop docker container

    Example:

        url = 'http://10.230.1.205:5502/command'
        data = {
            "stop": "SBC-cam9-14"
        }

    '''

    tasks_stop = []
    for image_name in docker_images:
        image_cfg = cfg[image_name]
        data = json.dumps({command:image_cfg["image_name"]})    
        url = 'http://' + image_cfg["ip"] + ':' + image_cfg["port"] + '/command'
        print("Request send to:", image_cfg["ip"])
        print("Target Image: ", image_name)
        print("Command:", command)
        print("Data: ",data)
        tasks_stop.append(asyncio.create_task(send_command(url, data)))

    await asyncio.gather(*tasks_stop)

async def run_n_update(docker_command, docker_images):
    ''' Method to run or update docker container 
    
    Example:

        server_image_dic = {
            "10.230.1.205" : ["SBC-cam6-8", "SBC-cam9-14"]
        }

        url = 'http://10.230.1.205:5502/command'

        data = 
            "run":{
                "type": "stream"/"watchdog",
                "json": ["0cd2f071-678c-48cb-af66-03d928ee6eac"],
                "path": "/home/everguard/eg_pipeline"
            }
            or
            "update":{
                "eg_pipeline_path":"/home/everguard/eg_pipeline",
                "sys_monitor_path":"/home/everguard/eg_pipeline"
            }
    ''' 

    server_image_dic = {}
    for image_name in docker_images:
        if cfg[image_name]["ip"] in server_image_dic.keys():
            server_image_dic[cfg[image_name]["ip"]].append(image_name)
        else:
            server_image_dic[cfg[image_name]["ip"]] = [image_name]

    tasks_run_n_update = []

    for server in server_image_dic.keys():
            img_cfg = cfg[server_image_dic[server][0]]
            url = 'http://' + server + ':' + img_cfg["port"] + '/command'
            if docker_command in ['stream', 'watchdog']:
                data = json.dumps({
                    "run":{
                        "type":docker_command,
                        "json":server_image_dic[server],
                        "path":img_cfg["eg_pipeline_path"]
                    }
                })
            else:
                data = json.dumps({
                    "update":{
                        "eg_pipeline_path":img_cfg["eg_pipeline_path"],
                        "sys_monitor_path":img_cfg["sys_monitor_path"]
                        }
                })
            tasks_run_n_update.append(asyncio.create_task(send_command(url, data)))
    
    res = await asyncio.gather(*tasks_run_n_update)
            

async def send_command(url, data):
    async with httpx.AsyncClient() as client:
        response = await client.post(url, data=data)
        res = response.content.decode("utf-8")
    return res


if __name__ == "__main__":
    with open("servers.json","r") as config:
        cfg = json.load(config)

    loop = asyncio.events.new_event_loop()
    img_n_status = loop.run_until_complete(
        get_status(command="check")
    )

    app.run(host='0.0.0.0', port=5502)
