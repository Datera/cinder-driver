#!/usr/bin/env python

from __future__ import print_function, unicode_literals, division

import argparse
import re
import sys
import threading
import uuid
import shlex
import subprocess
import time

from pprint import pprint

import requests
import paramiko

NAME_SITE = "http://www.mess.be/inickgenwuname.php"
BACKUP_SITE = "http://172.18.1.223/name"
RE = "From this day forward, I will be known as\.\.\.(.*?) -And"
# VM_IMAGE_NAME = "ubuntu-xenial-cloud"
VM_IMAGE_NAME2 = "barcelona-demo-image-2"
VM_CIRROS_IMAGE = "cirros-0.3.4-x86_64-uec"
VM_IMAGE_ID = "9cfff9af-4a77-4f29-b427-b2475e9ce386"
# VOL_IMAGE_ID = "dcb0c04e-8f0c-40a8-b45c-08981ea0122c"
OPENRC = "/local/devstack/openrc"
KEYFILE = "/opt/stack/.ssh/id_rsa"
KEY_NAME = "bdemo-key"
# USERNAME = "ubuntu"
USERNAME = "cirros"
PASSWORD = "cubswin:)"

UUID4_RE = ("([a-f0-9]{8}-?[a-f0-9]{4}-?4[a-f0-9]{3}-?[89ab]"
            "[a-f0-9]{3}-?[a-f0-9]{12})")
BDEV_RE = "(/dev/vd.\w?)"

# I'm feeling lazy and rebellious
debug = False
flag = False
keyfile = KEYFILE
keyname = KEY_NAME
print_lock = threading.Lock()


def demo_print(*args, **kwargs):
    x = 0
    cargs = []
    for arg in args:
        x += len(str(arg)) + 1
        cargs.append(arg)

    print_lock.acquire()
    if kwargs.get("heading"):
        char = "-"
        prefix = "----"
    else:
        char = "#"
        prefix = ""
    cargs.append(char)
    cargs.append(prefix)
    print()
    print(prefix, char * (x + 3), prefix)
    print(prefix, char, *cargs)
    print(prefix, char * (x + 3), prefix)
    print()
    print_lock.release()


def ssh(ip):
    s = paramiko.SSHClient()
    s.set_missing_host_key_policy(
        paramiko.AutoAddPolicy())
    k = paramiko.RSAKey.from_private_key_file(keyfile)
    t0 = time.time()
    while True:
        try:
            s.connect(
                hostname=ip,
                username=USERNAME,
                banner_timeout=600,
                pkey=k)
            # password=PASSWORD)
            break
        except paramiko.ssh_exception.NoValidConnectionsError:
            time.sleep(5)
        except paramiko.ssh_exception.SSHException:
            time.sleep(5)

    demo_print("{} connected in {} seconds".format(ip, time.time() - t0))
    return s


def vm_exec(name, ip, cmd, fail_ok=False):
    global debug
    s = ssh(ip)
    msg = "Executing command: {} on VM: {}".format(cmd, name)
    if debug:
        demo_print(msg)
    _, stdout, stderr = s.exec_command(cmd)
    exit_status = stdout.channel.recv_exit_status()
    result = None
    if int(exit_status) == 0:
        result = stdout.read()
    elif fail_ok:
        result = stderr.read()
    else:
        raise EnvironmentError(
            "Nonzero return code: {} stderr: {}".format(
                exit_status,
                stderr.read()))
    return result


def vm_dd(name, ip, infile, outfile, block_size, count, cirros=False):
    demo_print("Writing data to disk on {} with DD".format(name))
    if cirros:
        cmd = 'sudo dd if={} of={} bs={} count={} conv=fsync'.format(
            infile, outfile, block_size, count)
    else:
        cmd = 'sudo dd if={} of={} bs={} count={} oflag=direct'.format(
            infile, outfile, block_size, count)
    vm_exec(name, ip, cmd)


def vm_fio(name, ip, outfile, block_size, direct=1, copy=False):
    if copy:
        demo_print("Copying fio to VM {}".format(name))
        run_command(
            "scp -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no "
            "fio {}@{}:~/fio".format(USERNAME, ip))
    demo_print("Writing data to disk on {} with FIO".format(name))
    cmd = ('cat << EOF | sudo ./fio -\n[demo-job]\ndirect={}'
           '\nrw=randwrite\nbs={}\nfilename={}\nruntime=60000\n'
           'size=20GB\ntime_based=1\nEOF\n'.format(
               direct, block_size, outfile))
    # Pass fail_ok=True because this will always be cancelled before finishing
    # And we don't want that showing up in stdout
    vm_exec(name, ip, cmd, fail_ok=True)


def vm_turn_on_dio(name, ip):
    cmd = 'sudo sh -c "echo 1 > /proc/scsi/sg/allow_dio"'
    vm_exec(name, ip, cmd)


def vm_turn_off_upgr(name, ip):
    cmd = ('sudo sh -c \'echo "APT::Periodic::Unattended-Upgrade \\"0\\""; >> '
           '/etc/apt/apt.conf.d/10periodic\'')
    vm_exec(name, ip, cmd)


def get_wutang_name():
    uname = str(uuid.uuid4())
    payload = {"realname": uname}
    resp = requests.post(NAME_SITE, data=payload)
    return re.search(RE, resp.text.replace("\n", " ")).group(1).strip(
            ).replace("  ", " ")


def get_vm_name():
    return requests.get(BACKUP_SITE).json()['name']


def spin_up_vm_dat_backed(glance_img_id, vols, teardown=True):
    name = get_vm_name()
    vol_id = None
    demo_print("Spinning up VM {}".format(name))
    try:
        cmd1 = "cinder create 100 --image-id {} ".format(glance_img_id)
        result = run_os_command(cmd1)
        vol_id = re.search(UUID4_RE, result).group(1)
        vols.append((name, vol_id))

        demo_print("Poll for volume availability {}".format(vol_id))
        while True:
            result = run_os_command(
                "cinder show {} | grep status.*available".format(
                    vol_id), fail_ok=True)
            if result:
                break
            else:
                time.sleep(1)

        demo_print("Boot volume as VM '{}'".format(name))
        cmd2 = ("nova boot \"{}\" --flavor 3 --block-device source=volume,id={"
                "},dest=volume,size=100,shutdown=preserve,bootindex=0 "
                "--key-name {}")
        cmd2 = cmd2.format(name, vol_id, KEY_NAME)
        run_os_command(cmd2)
        ip = None

        demo_print("Poll for '{}' availability".format(name))
        while True:
            result = run_os_command(
                "nova list | grep \"{}.*Running.*private=\"".format(name),
                fail_ok=True)
            if result:
                if debug:
                    demo_print(result)
                ip = re.search("private=(.*) \|", result).group(1).strip()
                break
            else:
                time.sleep(2)

        demo_print("{}'s IP: {}".format(name, ip))
        demo_print("Trying to login to '{}'".format(name))
        vm_turn_on_dio(name, ip)
        vm_fio(name, ip, "/tmp/test.img", "256K")
        demo_print("VM {} is running and data is flowing!".format(name))
    finally:
        if teardown:
            run_os_command(
                "nova delete \"{}\"".format(name))
            while True:
                result = run_os_command(
                    "nova list | grep \"{}.*ACTIVE\"".format(name),
                    fail_ok=True)
                if result:
                    time.sleep(2)
                else:
                    break
            run_os_command(
                "cinder delete {}".format(vol_id))


def spin_up_vm_dat_scratch(glance_img_id, vols, teardown=True):
    name = get_vm_name()
    vol_id = None
    demo_print("Spinning up VM {}".format(name))
    try:
        result = run_os_command("cinder create 100")
        vol_id = re.search(UUID4_RE, result).group(1)
        vols.append((name, vol_id))

        demo_print("Poll for volume availability {}".format(vol_id))
        while True:
            result = run_os_command(
                "cinder show {} | grep status.*available".format(
                    vol_id), fail_ok=True)
            if result:
                break
            else:
                time.sleep(1)

        demo_print("Boot ephemeral VM '{}'".format(name))
        cmd2 = ("nova boot \"{}\" --flavor 3 --image {} --key-name {}")
        cmd2 = cmd2.format(name, glance_img_id, KEY_NAME)
        run_os_command(cmd2)
        ip = None

        demo_print("Poll for '{}' availability".format(name))
        while True:
            result = run_os_command(
                "nova list | grep \"{}.*Running.*private=\"".format(name),
                fail_ok=True)
            if result:
                if debug:
                    demo_print(result)
                ip = re.search("private=(.*) \|", result).group(1).strip()
                break
            else:
                time.sleep(2)

        demo_print("{}'s IP: {}".format(name, ip))

        demo_print("Attaching Volume {}".format(vol_id))
        attach_result = run_os_command(
            "nova volume-attach \"{}\" {}".format(name, vol_id))

        bdev = re.search(BDEV_RE, attach_result).group(1).strip()
        demo_print("Trying to login to '{}'".format(name))
        # vm_turn_off_upgr(name, ip)
        vm_turn_on_dio(name, ip)
        vm_fio(name, ip, bdev, "256K", direct=0, copy=True)
        # vm_dd(name, ip, "/dev/zero", bdev, "262144", 60000, cirros=True)
        demo_print("VM {} is running and data is flowing!".format(name))
    finally:
        if teardown:
            run_os_command(
                "nova delete \"{}\"".format(name))
            while True:
                result = run_os_command(
                    "nova list | grep \"{}.*ACTIVE\"".format(name),
                    fail_ok=True)
                if result:
                    time.sleep(2)
                else:
                    break
            run_os_command(
                "cinder delete {}".format(vol_id))


def run_os_command(cmd, fail_ok=False):
    global debug
    full_cmd = "bash -c 'source {} admin admin && {}'".format(OPENRC, cmd)
    return run_command(full_cmd, fail_ok)


def run_command(cmd, fail_ok=False):
    global debug
    if debug:
        demo_print("Running command: {}".format(cmd))
    try:
        output = subprocess.check_output(shlex.split(cmd))
        return output
    except subprocess.CalledProcessError as e:
        if fail_ok:
            if debug:
                demo_print(e)
            return ""
        else:
            raise


def run_demo(img_id, threads=5):
    demo_print("Starting Demo", heading=True)
    vols = []
    for _ in range(threads):
        thread = threading.Thread(target=spin_up_vm_dat_scratch,
                                  args=(img_id, vols),
                                  kwargs={'teardown': False})
        thread.daemon = True
        thread.start()
    while True:
        result = raw_input("Press 'a' to add another VM\n"
                           "Press 'c' to clean up\n"
                           "Press 's' to take snapshots\n"
                           "Press 'l' to list volumes\n"
                           "Press 'sl' to list snapshots\n")
        if result.lower() == 'a':
            thread = threading.Thread(
                target=spin_up_vm_dat_scratch,
                args=(img_id, vols),
                kwargs={'teardown': False})
            thread.daemon = True
            thread.start()
        elif result.lower() == 'c':
            clean()
            sys.exit(0)
        elif result.lower() == 's':
            snap(vols)
        elif result.lower() == 'l':
            pprint(vols)
        elif result.lower() == 'sl':
            pprint(get_snaps())
        else:
            print("Did not understand, please try again\n")


def get_snaps():
    result = run_os_command(
        "cinder snapshot-list")
    uuids = re.findall(UUID4_RE, result)
    return [{'snap': snap, 'vol': vol} for snap, vol in
            zip(uuids[::2], uuids[1::2])]


def snap(vols):
    for name, vol in vols:
        demo_print("Creating snapshot for {}".format(name))
        thread = threading.Thread(
            target=run_os_command,
            args=("cinder snapshot-create {} --force true".format(vol),))
        thread.daemon = True
        thread.start()


def get_image_id(image_name):
    demo_print("Starting System Prep", heading=True)
    # name = "testvm"
    # seconds = 2
    demo_print("Get image UUID")
    try:
        img_result = run_os_command(
            "glance image-list | grep {}".format(image_name))
        image_id = re.search(UUID4_RE, img_result).group(1)
        demo_print("Image UUID {}".format(image_id))
    except AttributeError:
        demo_print("Couldn't find image {}".format(image_name))
        sys.exit(1)
    return image_id

    # demo_print("Boot VM with image backed by Datera vol")
    # run_os_command(
    #     "nova boot --flavor 2 --block-device source=image,id={}"
    #     ",dest=volume,shutdown=preserve,bootindex=0,"
    #     "size=30 {}".format(image_id, name))
    # demo_print("Waiting for VM to boot")
    # while True:
    #     result = run_os_command(
    #         "nova list | grep \"{}.*Running.*private=\"".format(name),
    #         fail_ok=True)
    #     if result:
    #         break
    #     else:
    #         time.sleep(seconds)
    # demo_print("Delete VM, leave Datera vol")
    # run_os_command(
    #     "nova delete {}".format(name))
    # return get_vol_id()


def get_vol_id():
    demo_print("Get volume UUID")
    result = run_os_command(
        "cinder list")
    vol_id = re.search(UUID4_RE, result).group(1)
    return vol_id


def clean():
    demo_print("Cleaning up", heading=True)
    demo_print("Deleting VMs")
    result = run_os_command(
        "nova list")
    for vm in re.findall(UUID4_RE, result):
        threading.Thread(
            target=run_os_command,
            args=("nova delete {}".format(vm),)).start()
    time.sleep(20)
    while True:
        result = run_os_command("nova list | grep ACTIVE", fail_ok=True)
        if result:
            time.sleep(2)
        else:
            break
    demo_print("Deleting Volumes")
    result = run_os_command(
        "cinder list")

    def func(vol):
        global flag
        try:
            run_os_command(
                "cinder delete {} --cascade".format(vol))
        except subprocess.CalledProcessError:
            flag = True

    for vol in re.findall(UUID4_RE, result):
        threading.Thread(
            target=func, args=(vol,)).start()
    result = run_os_command(
        "cinder list")
    global flag
    if flag:
        time.sleep(10)
        for snap in get_snaps():
            run_os_command(
                "cinder snapshot-delete {} --force".format(snap))
        for vol in re.findall(UUID4_RE, result):
            run_os_command(
                "cinder force-delete {}".format(vol))
    demo_print("Finished cleaning")
    return 0


def main(args):
    global debug, keyfile, keyname
    if args.debug:
        debug = True
    if args.clean:
        sys.exit(clean())
    if args.prep:
        pass
        # vol_id = prep(args.image_name)
    if args.keyfile:
        keyfile = args.keyfile
    if args.keyname:
        keyname = args.keyname

    glance_id = get_image_id(args.image_name)
    run_demo(glance_id, threads=args.threads)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        "\n\nAfter the demo is up and running, use the folling keys to issue"
        " requests\n\n"
        "Press 'a' to add another VM\n"
        "Press 'c' to clean up\n"
        "Press 's' to take snapshots\n"
        "Press 'l' to list volumes\n")
    parser.add_argument("-t", "--threads", default=5, type=int,
                        help="Number of threads (1 VM per thread) to run the "
                             "demo with")
    parser.add_argument("-p", "--prep", action="store_true",
                        help="Prep machine after clean install")

    parser.add_argument("-c", "--clean", action="store_true",
                        help="Clean existing VMs and Cinder vols")
    parser.add_argument("-d", "--debug", action="store_true",
                        help="demo_print debug messages")
    parser.add_argument("-i", "--image-name", default=VM_CIRROS_IMAGE,
                        help="Glance image name to use for cloning")
    parser.add_argument("--keyfile", help="SSH keyfile location")
    parser.add_argument("--keyname", help="OpenStack SSH Key Name")
    parser.add_argument("--openrc", help="OpenRC file location")

    args = parser.parse_args()

    sys.exit(main(args))
