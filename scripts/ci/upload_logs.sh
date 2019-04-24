#!/bin/bash -xe

REF_NAME=$1
LOGDIR=/opt/stack/logs
cd  $HOME

mkdir -p $REF_NAME/logs/etc

PROJECTS="openstack-dev/devstack $PROJECTS"
PROJECTS="openstack/cinder $PROJECTS"
PROJECTS="openstack/glance $PROJECTS"
PROJECTS="openstack/glance_store $PROJECTS"
PROJECTS="openstack/horizon $PROJECTS"
PROJECTS="openstack/keystone $PROJECTS"
PROJECTS="openstack/keystonemiddleware $PROJECTS"
PROJECTS="openstack/nova $PROJECTS"
PROJECTS="openstack/neutron $PROJECTS"
PROJECTS="openstack/oslo.config $PROJECTS"
PROJECTS="openstack/oslo.db $PROJECTS"
PROJECTS="openstack/oslo.i18n $PROJECTS"
PROJECTS="openstack/oslo.messaging $PROJECTS"
PROJECTS="openstack/oslo.middleware $PROJECTS"
PROJECTS="openstack/oslo.rootwrap $PROJECTS"
PROJECTS="openstack/oslo.serialization $PROJECTS"
PROJECTS="openstack/oslo.vmware $PROJECTS"
PROJECTS="openstack/python-cinderclient $PROJECTS"
PROJECTS="openstack/python-glanceclient $PROJECTS"
PROJECTS="openstack/python-keystoneclient $PROJECTS"
PROJECTS="openstack/python-novaclient $PROJECTS"
PROJECTS="openstack/python-openstackclient $PROJECTS"
PROJECTS="openstack/requirements $PROJECTS"
PROJECTS="openstack/stevedore $PROJECTS"
PROJECTS="openstack/taskflow $PROJECTS"
PROJECTS="openstack/tempest $PROJECTS"
# devstack logs
cd ~/devstack
cp local.conf $HOME/$REF_NAME/logs/local.conf.txt
cp /opt/stack/devstacklog.txt $HOME/$REF_NAME/logs/devstacklog.txt

# Archive config files
for PROJECT in $PROJECTS; do
    proj=$(basename $PROJECT)
    if [ -d /etc/$proj ]; then
        sudo cp -r /etc/$proj $HOME/$REF_NAME/logs/etc/
    fi
done

# OS Service Logs
LOGFILES=$(systemctl list-unit-files --all | grep devstack@ | awk '{print $1}' | sed 's/\./ /' | awk '{print $1}')
echo "LOGFILES: $LOGFILES"
for log in $LOGFILES; do
    sudo journalctl --unit $log > $HOME/$REF_NAME/logs/$log.txt
done

# Add the commit id
cd /opt/stack/cinder
COMMIT_ID=$(git log --abbrev-commit --pretty=oneline -n1 | awk '{print $2}')
echo "commit_id: $COMMIT_ID"
echo "commit_id: $COMMIT_ID" >> console.log.out

# Tempest logs
cd /opt/stack/tempest
cp etc/tempest.conf  $HOME/$REF_NAME/logs/tempest.conf

cd $HOME
cp console.log.out  $HOME/$REF_NAME/console.log.out
cp console.log.out  $HOME/$REF_NAME/console.log.txt
# Tar it all up
#cd $REF_NAME
tar -zcvf $REF_NAME.tar.gz $REF_NAME
chown -R $USER:$USER $REF_NAME*
