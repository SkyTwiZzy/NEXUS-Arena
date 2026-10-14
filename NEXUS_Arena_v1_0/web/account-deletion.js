'use strict';
document.querySelector('#deleteForm').addEventListener('submit',async e=>{
 e.preventDefault();const f=e.target,b=f.querySelector('[type=submit]'),out=document.querySelector('#result');
 b.disabled=true;out.textContent='Обрабатываем запрос…';
 const email=f.elements.namedItem('email').value, password=f.elements.namedItem('password').value;
 try {
  let r=await fetch('/api/auth/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});
  if(!r.ok)throw new Error((await r.json()).detail||'Ошибка входа');
  r=await fetch('/api/account/delete',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({password})});
  if(!r.ok)throw new Error((await r.json()).detail||'Ошибка удаления');
  out.textContent='Аккаунт удалён. Вы больше не сможете в него войти.';f.reset();
 }catch(error){out.textContent=error.message;}finally{b.disabled=false;}
});
