import json
import requests
import argparse
import sys
import multiprocessing

def send_cmd(url,data):
    # function to send command to each client 
    response = requests.post(url, data = data)
    print(url,data)
    result = response.content.decode("utf-8")
    print(result)

def check_command(args):
    # check if command is valid
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
    
    jobs = []
    check_command(args)
    print("Command Accepted")
    
    
    if args.command in ['stream','watchdog','update']:
        # run pipeline and update code command should be run with a server manner
        # because there should be come wait time between two containers that run on the same server 
        
        server_image_dic = {}
        # first, group each container with its assigned server
        if args.docker == None:
            # run all docker container in the cfg file
            for image_name in cfg.keys():
                if cfg[image_name]["ip"] in server_image_dic.keys():
                    server_image_dic[cfg[image_name]["ip"]].append(image_name)
                else:
                    server_image_dic[cfg[image_name]["ip"]] = [image_name]
        else:
            # run specific container given by the imput command
            for image_name in list(args.docker):
                if cfg[image_name]["ip"] in server_image_dic.keys():
                    server_image_dic[cfg[image_name]["ip"]].append(image_name)
                else:
                    server_image_dic[cfg[image_name]["ip"]] = [image_name]

        # for each server send the command that contains which container(s) to run 
        for server in server_image_dic.keys():
            url = 'http://' + server + ':' + cfg[server_image_dic[server][0]]["port"] + '/command'
            if args.command == 'stream' or args.command == 'watchdog':
                data = json.dumps({"run":{"type":args.command,"json":server_image_dic[server],"path":cfg[server_image_dic[server][0]]["eg_pipeline_path"]}})
            else:
                data = json.dumps({"update":{"eg_pipeline_path":cfg[server_image_dic[server][0]]["eg_pipeline_path"],"sys_monitor_path":cfg[server_image_dic[server][0]]["sys_monitor_path"]}})
            print(data)
            p = multiprocessing.Process(target=send_cmd, args=(url,data))
            jobs.append(p)
            p.start()

        for job in jobs:
            p.join()
            
    # check and stop command
    else:
        if args.docker == None:
            # run all docker container in the cfg file
            for image_name in cfg.keys():
                image = cfg[image_name]
                if args.command == 'check':
                    data = json.dumps({"check":image["image_name"]})
                elif args.command == 'stop':
                    data = json.dumps({"stop":image["image_name"]})
                
                url = 'http://' + image["ip"] + ':' + image["port"] + '/command'
                print("Request send to:", image["ip"])
                if args.command != 'update':
                    print("Target Image: ", image_name)
                print("Command:", args.command)
                print("Data: ",data) 
                print()
                p = multiprocessing.Process(target=send_cmd, args=(url,data))
                jobs.append(p)
                p.start()
        else:
            # check/stop specific container given by the imput command
            for image_name in args.docker:
                image = cfg[image_name]
                if args.command == 'check':
                    data = json.dumps({"check":image["image_name"]})
                elif args.command == 'stop':
                    data = json.dumps({"stop":image["image_name"]})
                    
                url = 'http://' + image["ip"] + ':' + image["port"] + '/command'
                print("Request send to:", image["ip"])
                print("Command:", args.command)                    
                p = multiprocessing.Process(target=send_cmd, args=(url,data))
                jobs.append(p)
                p.start()
            for proc in jobs:
                proc.join()
     
        
    
