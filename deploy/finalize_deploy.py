import json,os,pathlib,subprocess
root=pathlib.Path('/opt/docker/hermes-talk')
lock=json.loads((root/'build-lock.json').read_text())
for target in ['bridge','runner']:
    subprocess.run(['docker','build','--target',target,'--build-arg','PYTHON_BASE='+lock['python_base'],
                    '-f','Dockerfile.secure','-t','local/hermes-talk-'+target+':secure-1','.'],cwd=root,check=True)
images={}
for target in ['bridge','runner']:
    image=json.loads(subprocess.check_output(['docker','image','inspect','local/hermes-talk-'+target+':secure-1'],text=True))[0]
    images[target]={'id':image['Id'],'size':image['Size']}
env=root/'.env'
env.write_text('BRIDGE_IMAGE='+images['bridge']['id']+'\nRUNNER_IMAGE='+images['runner']['id']+'\n')
os.chmod(env,0o600)
subprocess.run(['docker','compose','--env-file',str(env),'-f',str(root/'compose.yaml'),'up','-d'],check=True)
lock['images']=images
(root/'build-lock.json').write_text(json.dumps(lock,indent=2))
print(json.dumps(lock))
