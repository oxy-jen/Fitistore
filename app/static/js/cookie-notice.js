// From Uiverse.io by dldrs - https://uiverse.io/dldrs/mean-warthog-78
// MIT License, Copyright (c) dldrs (dldrs), dldrs (dldrs)
document.addEventListener('DOMContentLoaded',()=>{
  const notice=document.querySelector('[data-cookie-notice]');
  if(!notice)return;
  const dismiss=()=>{
    const secure=window.location.protocol==='https:'?'; Secure':'';
    document.cookie=`fity_cookie_notice_acknowledged=1; Max-Age=15552000; Path=/; SameSite=Lax${secure}`;
    notice.remove();
  };
  notice.querySelectorAll('[data-cookie-notice-accept],[data-cookie-notice-dismiss]').forEach(button=>{
    button.addEventListener('click',dismiss);
  });
});
