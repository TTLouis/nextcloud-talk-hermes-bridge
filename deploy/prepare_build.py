import json,pathlib,subprocess
root=pathlib.Path('/opt/docker/hermes-talk')
assert root.is_dir()
runtime=root/'runtime';runtime.mkdir(exist_ok=True)
source=runtime/'hermes'
if not source.exists():
    subprocess.run(['git','clone','--depth','1','--branch','v0.22.0','https://github.com/NousResearch/hermes-agent.git',str(source)],check=True)
commit=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD'],text=True).strip()
assert commit=='e06df9c2eae6901f4c88f464e0ae6e753c0428b8'
base='python@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170'
subprocess.run(['docker','pull',base],check=True)
(root/'build-lock.json').write_text(json.dumps({'python_base':base,'hermes_commit':commit,'hermes_version':'0.22.0'},indent=2))
print(json.dumps({'base':base,'hermes':commit}))
subprocess.run(['docker','build','--target','bridge','--build-arg','PYTHON_BASE='+base,'-f','Dockerfile.secure','-t','local/hermes-talk-bridge:secure-1','.'],cwd=root,check=True)
subprocess.run(['docker','build','--target','runner','--build-arg','PYTHON_BASE='+base,'-f','Dockerfile.secure','-t','local/hermes-talk-runner:secure-1','.'],cwd=root,check=True)
