import json
import requests
import argparse
import sys
import multiprocessing

def get_POST_data(args,image_name,image_cfg):
    data = None
    
    if args.command == 'check':
        data = json.dumps({"check":image_cfg["image_name"]})
    elif args.command == 'stop':
        data = json.dumps({"stop":image_cfg["image_name"]})
        
    return data

def send_cmd(image_name,image_cfg,args,url,data):
    response = requests.post(url, data = data)
    print(url,data)
    result = response.content.decode("utf-8")
    print(result)

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
    
    jobs = []
    check_command(args)
    print("Command Accepted")
    
    # run pipeline and update code command should be run with a server manner
    if args.command in ['stream','watchdog','update']:
        server_image_dic = {}
        if args.docker == None:
            for image_name in cfg.keys():
                if cfg[image_name]["ip"] in server_image_dic.keys():
                    server_image_dic[cfg[image_name]["ip"]].append(image_name)
                else:
                    server_image_dic[cfg[image_name]["ip"]] = [image_name]
        else:
            for image_name in list(args.docker):
                if cfg[image_name]["ip"] in server_image_dic.keys():
                    server_image_dic[cfg[image_name]["ip"]].append(image_name)
                else:
                    server_image_dic[cfg[image_name]["ip"]] = [image_name]


        for server in server_image_dic.keys():
            url = 'http://' + server + ':' + cfg[server_image_dic[server][0]]["port"] + '/command'
            if args.command == 'stream' or args.command == 'watchdog':
                data = json.dumps({"run":{"type":args.command,"json":server_image_dic[server],"path":cfg[server_image_dic[server][0]]["eg_pipeline_path"]}})
            else:
                data = json.dumps({"update":{"eg_pipeline_path":cfg[server_image_dic[server][0]]["eg_pipeline_path"],"sys_monitor_path":cfg[server_image_dic[server][0]]["sys_monitor_path"]}})
            print(data)
            p = multiprocessing.Process(target=send_cmd, args=(None,None,args,url,data))
            jobs.append(p)
            p.start()

        for job in jobs:
            p.join()
    # chekc and stop command
    else:
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
                p = multiprocessing.Process(target=send_cmd, args=(image_name,image,args,url,data))
                jobs.append(p)
                p.start()
        else:
            for image_name in args.docker:
                image = cfg[image_name]
                data = get_POST_data(args,image_name,image)
                url = 'http://' + image["ip"] + ':' + image["port"] + '/command'
                print("Request send to:", image["ip"])
                print("Command:", args.command)                    
                p = multiprocessing.Process(target=send_cmd, args=(image_name,image,args,url,data))
                jobs.append(p)
                p.start()
            for proc in jobs:
                proc.join()
     
        
    
