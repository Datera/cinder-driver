FROM registry.redhat.io/rhosp14/openstack-cinder-volume
MAINTAINER Datera Ecosystems Engineering g-ecosystems@datera.io

LABEL name="datera-openstack-cinder-volume-rhosp14" \
      maintainer="g-ecosystems@datera.io" \
      vendor="Datera" \
      version="1.0" \
      release="1" \
      summary="Red Hat OpenStack Platform 14.0 cinder-volume Datera Plugin" \
      description="Red Hat OpenStack Platform 14.0 cinder-volume Datera Plugin"

USER root

ARG version="2019.6.4.1"
RUN curl -L -o /tmp/driver.zip "https://github.com/Datera/cinder-driver/archive/${version}.zip"

RUN yum clean all
RUN yum-config-manager --enable rhel-7-server-openstack-14-rpms
RUN yum -y install unzip
RUN yum-config-manager --disable rhel-7-server-openstack-14-rpms

RUN cd /root && unzip /tmp/driver.zip

# License
RUN mkdir /licenses
RUN cp /root/cinder-driver-${version}/LICENSE /licenses

# Remove obsolete upstream driver
RUN rm -rf /usr/lib/python2.7/site-packages/cinder/volume/drivers/datera
# Copy driver files in place
RUN cp -rf /root/cinder-driver-${version}/src/datera /usr/lib/python2.7/site-packages/cinder/volume/drivers/

USER cinder
