import { createApp } from 'vue';
import { createRouter, createWebHistory } from 'vue-router';
import App from './App.vue';
import Lab from './views/Lab.vue';
import IncidentDetail from './views/IncidentDetail.vue';
import Environment from './views/Environment.vue';
import Incidents from './views/Incidents.vue';
import 'element-plus/dist/index.css';
import './style.css';

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/lab' },
    { path: '/lab', component: Lab },
    { path: '/environment', component: Environment },
    { path: '/incidents', component: Incidents },
    { path: '/incidents/:id', component: IncidentDetail },
    { path: '/:pathMatch(.*)*', redirect: '/lab' },
  ],
});

createApp(App).use(router).mount('#app');
