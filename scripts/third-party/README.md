# Creating the Docker container for certification

- Ideally, your host OS should be RHEL with an active subscription (see below)
- Create a new [Certification Project](https://connect.redhat.com/zones/red-hat-openstack-nfv/certify)
- Instructions under “Upload your Image” tab under the [project](E.g. https://connect.redhat.com/project/3666711/view) you created. 
- Create a Dockerfile
- authenticate
    - docker login registry.redhat.io
- this line is required in the Dockerfile (unless you already run on RHEL)
    - RUN subscription-manager register --username <Datera_partner_username> --password <Datera_partner_password> --auto-attach
- Build an image, e.g.
    - docker build -t datera_os_driver -f Dockerfile.2019.12.10.0.rhosp16 .
- List images
    - docker image list
		REPOSITORY                                                     TAG                 IMAGE ID            CREATED             SIZE
		datera_os_driver                                               latest              278ba72a8679        11 seconds ago      1.65GB
- Run that image, verify all is good
    - docker run -i -t 008c3fb1ebad /bin/bash
- Tag the image
    - docker tag 278ba72a8679 scan.connect.redhat.com/<ospid-id>/<container_name>:<version>, e.g.
    - docker tag 278ba72a8679 scan.connect.redhat.com/ospid-043f7e8e-e677-468a-8749-66eb2cf6f9ec/rhosp16-openstack-cinder-volume:2019.12.10.0
- Login
    - docker login -u unused scan.connect.redhat.com
    - use credentials from project page
- Publish
    - docker push scan.connect.redhat.com/<ospid-id>/<container_name>:<version>, e.g.
    - docker push scan.connect.redhat.com/ospid-043f7e8e-e677-468a-8749-66eb2cf6f9ec/datera_os_driver:2019.6.4.1
