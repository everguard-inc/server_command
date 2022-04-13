import json
import requests
import argparse
import sys
import multiprocessing

def get_POST_data(args,image_name,image_cfg):
    data = None
    if args.command == 'stream':
        data = json.dumps({'run':{"type":"stream","json":image_name,"path":image_cfg["eg_pipeline_path"]}})
    elif args.command == 'watchdog':
        data = json.dumps({'run':{"type":"watchdog","json":image_name,"path":image_cfg["eg_pipeline_path"]}})
    elif args.command == 'check':
        data = json.dumps({"check":image_cfg["image_name"]})
    elif args.command == 'update':
        data = json.dumps({"update":{"eg_pipeline_path":image_cfg["eg_pipeline_path"],"sys_monitor_path":image_cfg["sys_monitor_path"]}})
    elif args.command == 'stop':
        data = json.dumps({"stop":image_cfg["image_name"]})
        
    return data

def send_cmd(image_name,image_cfg,args,url,data):
    response = requests.post(url, data = data)
    print(url,data)
    if args.command == 'check':
        running = response.content.decode("utf-8")
        # TODO 
        # just return the result if it's running or not
        print("In Server: ",image_name)
        print("# Should Run: ", len(image_cfg["default_cfg"]))
        print("# Current Running: ",running)
            
    if args.command == 'stream' or args.command == 'watchdog':
        # TODO 
        # should check after running 
        running = response.content.decode("utf-8")
        print("In Server: ",image_name)
        print("Status: ", running)
    


def check_command(args):
    if args.command == None:
        print("No Command Found") 
        sys.exit(0)
    else:
        if args.command not in ['stream','check','watchdog','update','stop']:
            print("Command Name Error, Must be stream, watchdog, update, stop, check")
            sys.exit(0)

           

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument('-d','--docker', type=str, nargs='+')
    p.add_argument('-c','--command', type=str, default='check')
    
    # read the command and the cfg file
    args = p.parse_args()
    with open("images.json","r") as f:
        cfg = json.load(f)
    
    for k in cfg:
        print("Key: ",k)
        print(cfg[k])
        print()
    
    print(args)

    
    checking = {}    
    run_command_check = {}
    manager = multiprocessing.Manager()
    return_dic = manager.dict()
    jobs = []
    check_command(args)
    print("Command Accepted")
    # decide the target
    if args.docker == None:
        for image_name in cfg.keys():
            image = cfg[image_name]
            data = get_POST_data(args,image_name,image)
            url = 'http://' + image["ip"] + ':' + image["port"] + '/command'
            print("Request send to:", image["ip"])
            if args.command != 'update':
                print("Target Image: ", image_name)
            print("Command:", args.command)
            print("Data: ",data) 
            print()
            #p = multiprocessing.Process(target=send_cmd, args=(image_name,image,args,url,data))
            #jobs.append(p)
            #p.start()
    """
    else:
        for server_name in args.servers:
            server = cfg[server_name]
            data = get_POST_data(args,server)
            url = 'http://' + server["ip"] + ':' + server["port"] + '/command'
            print("Request send to:", server["ip"])
            print("Command:", args.command)
            p = multiprocessing.Process(target=send_cmd, args=(server_name,server,args,url,data))
            jobs.append(p)
            p.start()
    
    for proc in jobs:
        proc.join()
    """ 
        
    
