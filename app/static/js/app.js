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

  const homeScrollRails=[];
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
      requestAnimationFrame(()=>homeScrollRails.filter(rail=>rail.root.dataset.homeScrollRail==='Mamba products').forEach(rail=>rail.update()));
    }));
  }

  const reducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.querySelectorAll('[data-footer-rotator]').forEach(rotator=>{
    const slides=[...rotator.querySelectorAll('[data-footer-slide]')];
    if(slides.length<2||reducedMotion)return;
    let activeIndex=0;
    window.setInterval(()=>{
      slides[activeIndex].classList.remove('is-current');
      slides[activeIndex].setAttribute('aria-hidden','true');
      slides[activeIndex].inert=true;
      activeIndex=(activeIndex+1)%slides.length;
      slides[activeIndex].classList.add('is-current');
      slides[activeIndex].setAttribute('aria-hidden','false');
      slides[activeIndex].inert=false;
    },10000);
  });
  const carouselStates=[];
  document.querySelectorAll('[data-carousel]').forEach(carousel=>{
    const track=carousel.querySelector('[data-carousel-track]');
    if(!track)return;
    const arrows=Array.from(carousel.querySelectorAll('[data-carousel-direction]'));
    const update=()=>{
      const max=Math.max(0,track.scrollWidth-track.clientWidth);
      arrows.forEach(arrow=>{
        const direction=Number(arrow.dataset.carouselDirection||0);
        arrow.disabled=max<2||(direction<0?track.scrollLeft<=1:track.scrollLeft>=max-1);
      });
    };
    carouselStates.push({update});
    arrows.forEach(arrow=>arrow.addEventListener('click',()=>{
      const distance=Math.max(180,Math.floor(track.clientWidth*.82))*Number(arrow.dataset.carouselDirection||1);
      track.scrollBy({left:distance,behavior:reducedMotion?'auto':'smooth'});
    }));
    track.addEventListener('scroll',()=>requestAnimationFrame(update),{passive:true});
    update();
  });
  window.addEventListener('resize',()=>carouselStates.forEach(state=>state.update()),{passive:true});

  document.querySelectorAll('[data-home-scroll-rail]').forEach(root=>{
    const track=root.querySelector('[data-home-scroll-track]')||root.querySelector('.department-scroll');
    const dots=root.querySelector('[data-home-scroll-dots]');
    if(!track||!dots)return;
    let dotCount=0;
    let frame=0;
    const update=()=>{
      const maxScroll=Math.max(0,track.scrollWidth-track.clientWidth);
      const nextCount=maxScroll<2?1:Math.ceil(track.scrollWidth/Math.max(track.clientWidth,1));
      if(nextCount!==dotCount){
        dotCount=nextCount;
        dots.replaceChildren();
        for(let index=0;index<dotCount;index++){
          const dot=document.createElement('button');
          dot.type='button';
          dot.className='home-scroll-dot';
          dot.setAttribute('aria-label',`Scroll ${root.dataset.homeScrollRail} to position ${index+1} of ${dotCount}`);
          dot.addEventListener('click',()=>{
            const currentMax=Math.max(0,track.scrollWidth-track.clientWidth);
            track.scrollTo({left:dotCount<2?0:currentMax*index/(dotCount-1),behavior:reducedMotion?'auto':'smooth'});
          });
          dots.appendChild(dot);
        }
      }
      const active=dotCount<2?0:Math.round(track.scrollLeft/maxScroll*(dotCount-1));
      dots.querySelectorAll('button').forEach((dot,index)=>{
        if(index===active)dot.setAttribute('aria-current','true');else dot.removeAttribute('aria-current');
      });
    };
    const scheduleUpdate=()=>{
      if(frame)return;
      frame=requestAnimationFrame(()=>{frame=0;update()});
    };
    track.addEventListener('scroll',scheduleUpdate,{passive:true});
    homeScrollRails.push({root,update:scheduleUpdate});
    update();
  });
  window.addEventListener('resize',()=>homeScrollRails.forEach(rail=>rail.update()),{passive:true});

  const mostAdded=document.querySelector('[data-most-added]');
  if(mostAdded){
    const track=mostAdded.querySelector('[data-most-added-track]');
    const previous=mostAdded.querySelector('[data-most-added-prev]');
    const next=mostAdded.querySelector('[data-most-added-next]');
    const dots=mostAdded.querySelector('[data-most-added-dots]');
    const status=mostAdded.querySelector('[data-most-added-status]');
    if(track&&previous&&next&&dots){
      const cards=Array.from(track.children);
      let pageSize=0;
      let currentPage=0;
      let pages=[];
      const getPageSize=()=>window.matchMedia('(max-width: 600px)').matches?2:window.matchMedia('(max-width: 980px)').matches?3:4;
      const showPage=(index,{announce=false}={})=>{
        currentPage=Math.max(0,Math.min(index,pages.length-1));
        track.style.transform=`translate3d(${-currentPage*100}%,0,0)`;
        pages.forEach((page,pageIndex)=>{
          const active=pageIndex===currentPage;
          page.setAttribute('aria-hidden',String(!active));
          if(active)page.removeAttribute('inert');else page.setAttribute('inert','');
        });
        previous.disabled=currentPage===0;
        next.disabled=currentPage>=pages.length-1;
        dots.querySelectorAll('button').forEach((dot,dotIndex)=>{
          if(dotIndex===currentPage)dot.setAttribute('aria-current','page');else dot.removeAttribute('aria-current');
        });
        if(announce&&status)status.textContent=`Page ${currentPage+1} of ${pages.length}`;
      };
      const buildPages=()=>{
        const nextPageSize=getPageSize();
        if(nextPageSize===pageSize)return;
        pageSize=nextPageSize;
        currentPage=0;
        track.replaceChildren();
        pages=[];
        for(let offset=0;offset<cards.length;offset+=pageSize){
          const page=document.createElement('div');
          page.className='most-added-page';
          cards.slice(offset,offset+pageSize).forEach(card=>page.appendChild(card));
          track.appendChild(page);
          pages.push(page);
        }
        dots.replaceChildren();
        pages.forEach((_,index)=>{
          const dot=document.createElement('button');
          dot.type='button';
          dot.className='most-added-dot';
          dot.setAttribute('aria-label',`Go to Most Added to Cart page ${index+1}`);
          dot.addEventListener('click',()=>showPage(index,{announce:true}));
          dots.appendChild(dot);
        });
        const maxHeight=Math.max(0,...pages.map(page=>page.scrollHeight));
        pages.forEach(page=>page.style.minHeight=`${maxHeight}px`);
        mostAdded.classList.add('is-ready');
        showPage(currentPage);
      };
      previous.addEventListener('click',()=>showPage(currentPage-1,{announce:true}));
      next.addEventListener('click',()=>showPage(currentPage+1,{announce:true}));
      track.addEventListener('keydown',event=>{
        if(event.key!=='ArrowLeft'&&event.key!=='ArrowRight')return;
        event.preventDefault();
        showPage(currentPage+(event.key==='ArrowRight'?1:-1),{announce:true});
      });
      let touchStartX=null;
      track.addEventListener('touchstart',event=>{touchStartX=event.changedTouches[0]?.clientX??null},{passive:true});
      track.addEventListener('touchend',event=>{
        if(touchStartX===null)return;
        const delta=event.changedTouches[0].clientX-touchStartX;
        touchStartX=null;
        if(Math.abs(delta)>45)showPage(currentPage+(delta<0?1:-1),{announce:true});
      },{passive:true});
      buildPages();
      window.addEventListener('resize',buildPages,{passive:true});
    }
  }

  const marquees=Array.from(document.querySelectorAll('.featured-showcase .product-marquee,.promo-showcase .product-marquee'));
  const updateMarquees=()=>marquees.forEach(marquee=>{
    const track=marquee.querySelector('.marquee-track');
    const firstGroup=track?.querySelector('.marquee-group:not([aria-hidden="true"])');
    if(!track||!firstGroup||!firstGroup.children.length)return;
    marquee.classList.remove('marquee-static');
    let addedBatches=0;
    while(track.scrollWidth<marquee.clientWidth*1.4&&addedBatches<16){
      for(let copy=0;copy<4;copy++){
        const clone=firstGroup.cloneNode(true);
        clone.setAttribute('aria-hidden','true');
        clone.setAttribute('inert','');
        clone.querySelectorAll('[id]').forEach(element=>element.removeAttribute('id'));
        clone.querySelectorAll('[for]').forEach(element=>element.removeAttribute('for'));
        track.appendChild(clone);
      }
      addedBatches++;
    }
  });
  updateMarquees();
  window.addEventListener('resize',updateMarquees,{passive:true});

  const categoryCards=document.querySelectorAll('.category-showcase .department-card');
  if(categoryCards.length&&'IntersectionObserver' in window&&!reducedMotion){
    const revealObserver=new IntersectionObserver(entries=>entries.forEach(entry=>{
      if(entry.isIntersecting){entry.target.classList.add('is-visible');revealObserver.unobserve(entry.target)}
    }),{threshold:.12,rootMargin:'0px 0px -20px 0px'});
    categoryCards.forEach(card=>{card.classList.add('category-reveal-ready');revealObserver.observe(card)});
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
  async function copyShareUrl(url){
    if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(url);return}
    const field=document.createElement('textarea');
    field.value=url;
    field.setAttribute('readonly','');
    field.style.position='fixed';
    field.style.opacity='0';
    document.body.appendChild(field);
    field.select();
    const copied=document.execCommand('copy');
    field.remove();
    if(!copied)throw new Error('Copy command unavailable');
  }
  document.addEventListener('click',async event=>{
    const action=event.target.closest('[data-share-native],[data-share-copy],[data-share-tiktok],[data-share-facebook]');
    if(!action)return;
    const share=action.closest('[data-product-share],[data-social-share]');
    if(!share)return;
    const url=share.dataset.shareUrl||window.location.href;
    const status=share.querySelector('[data-share-status]');
    const announce=message=>{if(status)status.textContent=message};
    if(action.hasAttribute('data-share-facebook')){
      const facebookUrl='https://www.facebook.com/sharer/sharer.php?u='+encodeURIComponent(url);
      window.open(facebookUrl,'_blank','noopener,noreferrer');
      announce('Facebook opened with a large clickable image preview. Tapping the preview opens this page.');
      return;
    }
    if(action.hasAttribute('data-share-copy')){
      try{await copyShareUrl(url);announce('Product link copied.')}
      catch(error){announce('Copy was unavailable. You can copy the product address from your browser.')}
      return;
    }
    if(action.hasAttribute('data-share-tiktok')){
      const productDetail=share.closest('.product-detail');
      const productCard=share.closest('.product-card');
      const image=productDetail?.querySelector('#detail-product-image')||productCard?.querySelector('.product-image img');
      const imageUrl=image?.currentSrc||image?.src||share.dataset.shareImage||'';
      if(!imageUrl){announce('This product has no shareable image.');return;}
      try{
        const response=await fetch(imageUrl,{mode:'cors',credentials:'omit'});
        if(!response.ok)throw new Error('Image download failed');
        const blob=await response.blob();
        if(!blob.type.startsWith('image/'))throw new Error('Product image could not be read');
        const extension=({'image/jpeg':'jpg','image/png':'png','image/webp':'webp','image/gif':'gif'})[blob.type]||'img';
        const file=new File([blob],'fity-product.'+extension,{type:blob.type});
        const canShareFile=typeof navigator.share==='function'&&(!navigator.canShare||navigator.canShare({files:[file]}));
        if(canShareFile){
          await navigator.share({files:[file],title:share.dataset.shareTitle||'FityStores product',text:share.dataset.shareText||url});
          announce('Choose TikTok in the share sheet, then review and post the image.');
        }else{
          const objectUrl=URL.createObjectURL(blob);
          const download=document.createElement('a');download.href=objectUrl;download.download='fity-product.'+extension;document.body.appendChild(download);download.click();download.remove();
          window.setTimeout(()=>URL.revokeObjectURL(objectUrl),30000);
          announce('Image downloaded. Open TikTok and upload it to a post.');
        }
      }catch(error){
        if(error&&error.name==='AbortError')announce('Sharing cancelled.');
        else{window.open(imageUrl,'_blank','noopener');announce('Could not prepare the image. Save it from the opened tab, then upload it in TikTok.');}
      }
      return;
    }
    if(typeof navigator.share==='function'){
      try{await navigator.share({title:share.dataset.shareTitle||'FityStores product',text:share.dataset.shareText||'',url});announce('Share sheet closed.')}
      catch(error){
        try{await copyShareUrl(url);announce(error&&error.name==='AbortError'?'Sharing cancelled. Product link copied.':'Sharing unavailable. Product link copied.')}
        catch(copyError){announce('Sharing is unavailable. You can copy the product address from your browser.')}
      }
    }else{
      try{await copyShareUrl(url);announce('Native sharing is unavailable. Product link copied.')}
      catch(error){announce('Sharing is unavailable. You can copy the product address from your browser.')}
    }
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

