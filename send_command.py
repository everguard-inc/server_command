import json
import requests
import argparse
import sys
import multiprocessing

def get_POST_data(args,dic):
    data = None
    if args.command == 'stream':
        data = json.dumps({'run':{"type":"stream","json":dic["default_cfg"],"path":dic["eg_pipeline_path"]}})
    elif args.command == 'watchdog':
        data = json.dumps({'run':{"type":"watchdog","json":dic["default_cfg"],"path":dic["eg_pipeline_path"]}})
    elif args.command == 'check':
        data = json.dumps({"check":True})
    elif args.command == 'update':
        data = json.dumps({"update":{"eg_pipeline_path":dic["eg_pipeline_path"],"sys_monitor_path":dic["sys_monitor_path"]}})
    elif args.command == 'stop':
        data = json.dumps({"stop":True})
        
    return data

def send_cmd(servername,servercfg,args,url,data):
    response = requests.post(url, data = data)
    print(url,data)
    if args.command == 'check':
        running = response.content.decode("utf-8")
        print("In Server: ",servername)
        print("# Should Run: ", len(servercfg["default_cfg"]))
        print("# Current Running: ",running)
            
    if args.command == 'stream' or args.command == 'watchdog':
        running = response.content.decode("utf-8")
        print("In Server: ",servername)
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
    p.add_argument('-s','--servers', type=str, nargs='+')
    p.add_argument('-c','--command', type=str, default='check')
    
    # read the command and the cfg file
    args = p.parse_args()
    #print(args.command,args.servers)
    with open("servers.json","r") as f:
        cfg = json.load(f)
         
    checking = {}    
    run_command_check = {}
    manager = multiprocessing.Manager()
    return_dic = manager.dict()
    jobs = []
    
    check_command(args)
    print("Command Accepted")
    # decide the target
    if args.servers == None:
        for k in cfg.keys():
            server = cfg[k]
            data = get_POST_data(args,server)
            url = 'http://' + server["ip"] + ':' + server["port"] + '/command'
            print("Request send to:", server["ip"])
            print("Command:", args.command)
            p = multiprocessing.Process(target=send_cmd, args=(k,server,args,url,data))
            jobs.append(p)
            p.start()
            
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
    
        
        
    
