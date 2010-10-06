from os.path import join, dirname

from django.conf.urls.defaults import *


urlpatterns = patterns('rhino.views',
    url(r'^$', 'home'),
)

urlpatterns += patterns('',
    url(r'^static/rhino/(?P<path>.*)$', 'django.views.static.serve',
        {'document_root': join(dirname(__file__), 'static')},
        name='rhino-static'),
)
