document.addEventListener('DOMContentLoaded',()=>{
  const button=document.querySelector('.menu-toggle');
  const nav=document.getElementById('nav');
  if(button&&nav){button.addEventListener('click',()=>{const open=nav.classList.toggle('open');button.setAttribute('aria-expanded',open?'true':'false')})}

  const grid=document.getElementById('product-grid');
  const loadMore=document.getElementById('load-more');
  const status=document.getElementById('load-status');
  const empty=document.getElementById('shop-empty');
  let loading=false;
  const seen=new Set(Array.from(document.querySelectorAll('[data-product-card]')).map(card=>card.dataset.productCard));
  async function loadProducts(){
    if(!grid||loading||grid.dataset.hasNext!=='true')return;
    loading=true;
    if(status)status.textContent='Loading more products...';
    const params=new URLSearchParams(window.location.search);
    params.set('page',grid.dataset.nextPage||'2');
    try{
      const response=await fetch(`${grid.dataset.apiUrl}?${params.toString()}`,{headers:{'Accept':'application/json'}});
      if(!response.ok)throw new Error('Product request failed');
      const data=await response.json();
      const holder=document.createElement('div');
      holder.innerHTML=data.html||'';
      holder.querySelectorAll('[data-product-card]').forEach(card=>{
        if(seen.has(card.dataset.productCard))return;
        seen.add(card.dataset.productCard);
        grid.appendChild(card);
      });
      grid.dataset.hasNext=data.has_next?'true':'false';
      grid.dataset.nextPage=data.next_page||'';
      if(empty)empty.hidden=grid.children.length>0;
      if(loadMore)loadMore.hidden=!data.has_next;
      if(status)status.textContent=data.has_next?'More products will load as you scroll.':'All matching products are loaded.';
    }catch(error){
      if(status)status.textContent='Could not load more products. Try again.';
      if(loadMore){loadMore.hidden=false;loadMore.innerHTML='<svg class="icon"><use href="#icon-refresh"></use></svg><span>Retry</span>';}
    }finally{loading=false;}
  }
  if(loadMore){loadMore.addEventListener('click',loadProducts)}
  if(grid&&'IntersectionObserver' in window){
    const sentinel=document.createElement('div');
    sentinel.className='scroll-sentinel';
    grid.after(sentinel);
    const observer=new IntersectionObserver(entries=>{if(entries.some(entry=>entry.isIntersecting))loadProducts()},{rootMargin:'700px 0px'});
    observer.observe(sentinel);
  }

  const mambaButtons=document.querySelectorAll('[data-mamba-filter]');
  const mambaProducts=document.querySelectorAll('[data-mamba-category]');
  const mambaEmpty=document.getElementById('mamba-empty');
  if(mambaButtons.length&&mambaProducts.length){
    mambaButtons.forEach(btn=>btn.addEventListener('click',()=>{
      const filter=btn.dataset.mambaFilter||'all';
      let shown=0;
      mambaButtons.forEach(item=>item.classList.toggle('active',item===btn));
      mambaProducts.forEach(item=>{
        const match=filter==='all'||item.dataset.mambaCategory===filter;
        item.hidden=!match;
        if(match)shown++;
      });
      if(mambaEmpty)mambaEmpty.hidden=shown>0;
    }));
  }

  const imageInput=document.getElementById('product-images');
  const preview=document.getElementById('image-preview');
  if(imageInput&&preview){
    imageInput.addEventListener('change',()=>{
      preview.innerHTML='';
      Array.from(imageInput.files||[]).forEach(file=>{
        if(!file.type.startsWith('image/'))return;
        const img=document.createElement('img');
        img.alt=file.name;
        img.src=URL.createObjectURL(file);
        img.onload=()=>URL.revokeObjectURL(img.src);
        preview.appendChild(img);
      });
    });
  }

  document.querySelectorAll('[data-copy-link]').forEach(button=>{
    button.addEventListener('click',async()=>{
      const link=button.dataset.copyLink||'';
      try{
        await navigator.clipboard.writeText(link);
        button.textContent='Copied';
      }catch(error){
        const input=button.closest('.share-link-box')?.querySelector('input');
        if(input){input.select();document.execCommand('copy');button.textContent='Copied';}
      }
      setTimeout(()=>{button.textContent='Copy'},1200);
    });
  });
  const onlineImageUrls=document.getElementById('online-image-urls');
  const addOnlineImageUrl=document.getElementById('add-online-image-url');
  if(onlineImageUrls&&addOnlineImageUrl){
    const updateRemoveButtons=()=>{
      const rows=Array.from(onlineImageUrls.querySelectorAll('.online-image-url-row'));
      rows.forEach(row=>{
        const remove=row.querySelector('[data-remove-online-image-url]');
        if(remove)remove.hidden=rows.length===1;
      });
    };
    addOnlineImageUrl.addEventListener('click',()=>{
      const row=document.createElement('div');
      row.className='online-image-url-row';
      const label=document.createElement('label');
      label.appendChild(document.createTextNode('Cloudinary image URL'));
      const input=document.createElement('input');
      input.type='url';
      input.name='online_image_url';
      input.placeholder='https://res.cloudinary.com/...';
      label.appendChild(input);
      const remove=document.createElement('button');
      remove.type='button';
      remove.className='btn';
      remove.dataset.removeOnlineImageUrl='';
      remove.setAttribute('aria-label','Remove this image URL');
      remove.textContent='Remove';
      row.append(label,remove);
      onlineImageUrls.appendChild(row);
      updateRemoveButtons();
      input.focus();
    });
    onlineImageUrls.addEventListener('click',event=>{
      const remove=event.target.closest('[data-remove-online-image-url]');
      if(!remove)return;
      remove.closest('.online-image-url-row').remove();
      updateRemoveButtons();
    });
    updateRemoveButtons();
  }

});

