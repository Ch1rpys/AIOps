import { createApp } from 'vue';
import { createRouter, createWebHistory } from 'vue-router';
import App from './App.vue';
import Lab from './views/Lab.vue';
import IncidentDetail from './views/IncidentDetail.vue';
import Environment from './views/Environment.vue';
import Evaluation from './views/Evaluation.vue';
import Incidents from './views/Incidents.vue';
import RecoveryCardPage from './views/RecoveryCardPage.vue';
import Compiler from './views/Compiler.vue';
import Twin from './views/Twin.vue';
import 'element-plus/dist/index.css';
import './style.css';

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/lab' },
    { path: '/lab', component: Lab },
    { path: '/environment', component: Environment },
    { path: '/incidents', component: Incidents },
    { path: '/incidents/:id/card', component: RecoveryCardPage },
    { path: '/incidents/:id', component: IncidentDetail },
    { path: '/evaluation', component: Evaluation },
    { path: '/compiler', component: Compiler },
    { path: '/twin', component: Twin },
    { path: '/:pathMatch(.*)*', redirect: '/lab' },
  ],
});

createApp(App).use(router).mount('#app');
