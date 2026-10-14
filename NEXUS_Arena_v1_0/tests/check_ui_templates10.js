const fs=require('fs'); const vm=require('vm');const assert=require('assert');
const path=require('path');
let src=fs.readFileSync(path.join(__dirname,'..','web','app.js'),'utf8');
src=src.slice(0,src.indexOf("document.addEventListener('click'"));
const ctx={console,setInterval:()=>0,clearInterval:()=>0,setTimeout,clearTimeout,Date,URL,Number,Math,JSON,Array,Object,String};
vm.createContext(ctx);vm.runInContext(src,ctx);
const user={id:1,nickname:'TestPerson',email:'a@b.test',mlbb_id:'3421',server_id:'999',game_role:'Mid',elo:1300,wins:11,losses:4,staff_role:'PLAYER',is_admin:0,is_moderator:0,kda:{kills:21,deaths:7,assists:30},kd:{kills:21,deaths:7,ratio:3},rank:{is_calibrating:false,games_played:15},ratings:{'1v1':{elo:1100,wins:2,losses:2,rank:{is_calibrating:true,games_played:4,remaining:6}},'2v2':{elo:975,wins:0,losses:1,rank:{is_calibrating:true,games_played:1,remaining:9}},'5v5':{elo:1300,wins:11,losses:4,rank:{is_calibrating:false,name:'Фантом'}}}};
ctx.TEST=user;vm.runInContext('state.user=TEST',ctx);
function render(name, extra='') {return vm.runInContext(extra+name+'()',ctx)}
let profile=render('profilePage');assert(profile.includes('Ранги по режимам'));assert(profile.includes('K/D'));assert(profile.includes('3.00'));assert(profile.includes('Калибровка 4/10'));
let home=render('arenaPage','state.news=[];');assert(home.includes('Ранги по режимам'));
let lb=render('leaderboardPage',"state.ratingMode='2v2';state.leaderboard=[];");assert(lb.includes('Дуэт 2×2'));
let lobby=render('lobbyPage',"state.lobby={lobby:{id:1,leader_id:1,mode:'2v2',status:'OPEN'},members:[{id:1,nickname:'TestPerson',wins:0,losses:0,elo:1000}],invites:[]};");assert(lobby.includes('2×2'));assert(lobby.includes('НАЧАТЬ ПОИСК'));
let match=render('matchesPage',"state.matches=[{id:7,mode:'1v1',status:'READY_CHECK',ready_deadline:2000000000,created_at:100000,ready_count:1,required_ready:2,players:[{id:1,nickname:'TestPerson',side:0,captain:1,ready:1},{id:2,nickname:'Opponent',side:1,captain:1,ready:0}],host:{nickname:'TestPerson',mlbb_id:'3421',server_id:'999'},evidence:[],kda:[]}];");assert(match.includes('Готовы 1/2'));assert(match.includes('Дуэль 1×1'));
console.log('HTML_TEMPLATES_OK');
