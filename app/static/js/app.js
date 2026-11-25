document.addEventListener('error',event=>{
  const image=event.target;
  if(!(image instanceof HTMLImageElement)||!image.hasAttribute('data-product-image-fallback')||image.dataset.fallbackApplied)return;
  const fallback=image.dataset.fallbackSrc;
  if(!fallback||image.src===fallback)return;
  image.dataset.fallbackApplied='true';
  image.src=fallback;
},true);

document.addEventListener('DOMContentLoaded',()=>{
  const button=document.querySelector('.menu-toggle');
  const nav=document.getElementById('nav');
  if(button&&nav){button.addEventListener('click',()=>{const open=nav.classList.toggle('open');button.setAttribute('aria-expanded',open?'true':'false')})}

  document.querySelectorAll('[data-search-autocomplete]').forEach(form=>{
    const input=form.querySelector('input[name="q"]');
    const list=form.querySelector('[data-search-suggestions]');
    if(!input||!list)return;
    let timer=0,controller=null,activeIndex=-1,suggestions=[];
    const close=()=>{
      list.hidden=true;
      input.setAttribute('aria-expanded','false');
      input.removeAttribute('aria-activedescendant');
      activeIndex=-1;
    };
    const showMessage=message=>{
      list.replaceChildren();
      const option=document.createElement('div');
      option.className='shop-search-suggestion-message';
      option.setAttribute('role','option');
      option.setAttribute('aria-disabled','true');
      option.textContent=message;
      list.appendChild(option);
      list.hidden=false;
      input.setAttribute('aria-expanded','true');
      input.removeAttribute('aria-activedescendant');
      activeIndex=-1;
      suggestions=[];
    };
    const setActive=index=>{
      const options=Array.from(list.querySelectorAll('[role="option"]:not([aria-disabled="true"])'));
      if(!options.length)return;
      activeIndex=(index+options.length)%options.length;
      options.forEach((option,optionIndex)=>option.setAttribute('aria-selected',optionIndex===activeIndex?'true':'false'));
      input.setAttribute('aria-activedescendant',options[activeIndex].id);
      options[activeIndex].scrollIntoView({block:'nearest'});
    };
    const render=items=>{
      suggestions=items;
      activeIndex=-1;
      list.replaceChildren();
      if(!items.length){showMessage('No matches yet. Press Enter to search the catalogue.');return;}
      items.forEach((item,index)=>{
        const option=document.createElement('a');
        option.className='shop-search-suggestion';
        option.id=`${input.id}-option-${index}`;
        option.href=item.url;
        option.setAttribute('role','option');
        option.setAttribute('aria-selected','false');
        option.tabIndex=-1;
        const image=document.createElement('img');
        image.src=item.image_url;
        image.alt=item.image_alt||'';
        image.loading='lazy';
        const copy=document.createElement('span');
        copy.className='shop-search-suggestion-copy';
        const name=document.createElement('strong');
        name.textContent=item.name;
        const category=document.createElement('small');
        category.textContent=item.category;
        const meta=document.createElement('span');
        meta.className='shop-search-suggestion-meta';
        const price=document.createElement('span');
        price.textContent=item.price;
        const availability=document.createElement('span');
        availability.className=item.availability==='Out of stock'?'is-unavailable':'is-available';
        availability.textContent=item.availability;
        meta.append(price,availability);
        copy.append(name,category,meta);
        option.append(image,copy);
        list.appendChild(option);
      });
      list.hidden=false;
      input.setAttribute('aria-expanded','true');
    };
    const search=async()=>{
      const query=input.value.trim();
      if(query.length<2){close();return;}
      if(controller)controller.abort();
      controller=new AbortController();
      const params=new URLSearchParams({q:query});
      const group=form.querySelector('[name="category_group"]');
      const category=form.querySelector('[name="category"]');
      if(group?.value)params.set('category_group',group.value);
      if(category?.value)params.set('category',category.value);
      try{
        const response=await fetch(`${form.dataset.suggestionsUrl}?${params}`,{headers:{'Accept':'application/json'},signal:controller.signal});
        if(!response.ok)throw new Error('Search suggestions unavailable');
        const data=await response.json();
        if(input.value.trim()!==query)return;
        render(data.suggestions||[]);
      }catch(error){
        if(error.name!=='AbortError'&&input.value.trim()===query)showMessage('Suggestions are unavailable right now. Press Enter to search.');
      }
    };
    input.addEventListener('input',()=>{
      window.clearTimeout(timer);
      if(controller)controller.abort();
      suggestions=[];
      close();
      if(input.value.trim().length>=2)timer=window.setTimeout(search,180);
    });
    input.addEventListener('focus',()=>{if(suggestions.length){list.hidden=false;input.setAttribute('aria-expanded','true')}});
    input.addEventListener('keydown',event=>{
      if(event.key==='Escape'&&!list.hidden){event.preventDefault();close();return;}
      if(list.hidden)return;
      if(event.key==='ArrowDown'){event.preventDefault();setActive(activeIndex+1)}
      else if(event.key==='ArrowUp'){event.preventDefault();setActive(activeIndex<0?suggestions.length-1:activeIndex-1)}
      else if(event.key==='Enter'&&activeIndex>=0){event.preventDefault();list.querySelectorAll('[role="option"]:not([aria-disabled="true"])')[activeIndex]?.click()}
    });
    form.addEventListener('submit',()=>close());
    document.addEventListener('pointerdown',event=>{if(!form.contains(event.target))close()});
    form.querySelectorAll('select[name="category_group"]').forEach(select=>select.addEventListener('change',()=>{if(input.value.trim().length>=2)search();else close()}));
  });

  const grid=document.getElementById('product-grid');
  const loadMore=document.getElementById('load-more');
  const status=document.getElementById('load-status');
  const empty=document.getElementById('shop-empty');
  let loading=false;
  const seen=new Set(Array.from(document.querySelectorAll('[data-product-card]')).map(card=>card.dataset.productCard));
  const categorySections=Array.from(document.querySelectorAll('[data-shop-category-section]'));
  document.querySelectorAll('[data-shop-category-toggle]').forEach(button=>{
    button.addEventListener('click',()=>{
      const items=document.getElementById(button.getAttribute('aria-controls'));
      if(!items)return;
      const expanded=button.getAttribute('aria-expanded')==='true';
      items.hidden=expanded;
      button.setAttribute('aria-expanded',String(!expanded));
    });
  });
  function updateCategoryPreview(section){
    const productGrid=section.querySelector('[data-shop-preview-grid]');
    if(!productGrid)return;
    const columns=getComputedStyle(productGrid).gridTemplateColumns.split(/\s+/).filter(Boolean).length||1;
    const visibleLimit=columns*2;
    const mobileCarousel=window.matchMedia('(max-width: 760px)').matches;
    const cards=Array.from(productGrid.querySelectorAll('[data-product-card]'));
    cards.forEach((card,index)=>{card.hidden=!mobileCarousel&&index>=visibleLimit});
    if(mobileCarousel)productGrid.setAttribute('tabindex','0');
    else productGrid.removeAttribute('tabindex');
    const showMore=section.querySelector('.shop-section-more');
    if(showMore)showMore.hidden=Number(section.dataset.totalProducts||0)<=(mobileCarousel?2:visibleLimit);
  }
  const updateCategoryPreviews=()=>categorySections.forEach(updateCategoryPreview);
  updateCategoryPreviews();
  let shopLayoutFrame=false;
  const scheduleShopResizeUpdate=()=>{
    if(shopLayoutFrame)return;
    shopLayoutFrame=true;
    requestAnimationFrame(()=>{
      shopLayoutFrame=false;
      updateCategoryPreviews();
    });
  };
  window.addEventListener('resize',scheduleShopResizeUpdate,{passive:true});
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
  document.querySelectorAll('[data-upcoming-spotlight]').forEach(spotlight=>{
    const slides=Array.from(spotlight.querySelectorAll('[data-upcoming-spotlight-slide]'));
    if(!slides.length)return;
    const previous=spotlight.querySelector('[data-upcoming-spotlight-prev]');
    const next=spotlight.querySelector('[data-upcoming-spotlight-next]');
    const status=spotlight.querySelector('[data-upcoming-spotlight-status]');
    const interval=Math.max(1000,Number(spotlight.dataset.interval)||5000);
    let activeIndex=0;
    let timer=0;
    let paused=false;
    const stop=()=>{if(timer){window.clearTimeout(timer);timer=0}};
    const show=index=>{
      activeIndex=(index+slides.length)%slides.length;
      slides.forEach((slide,slideIndex)=>{
        const active=slideIndex===activeIndex;
        slide.hidden=!active;
        slide.setAttribute('aria-hidden',String(!active));
        slide.inert=!active;
      });
      if(status)status.textContent=`${activeIndex+1} / ${slides.length}`;
      schedule();
    };
    const schedule=()=>{
      stop();
      if(slides.length<2||reducedMotion||paused||document.hidden)return;
      timer=window.setTimeout(()=>show(activeIndex+1),interval);
    };
    previous?.addEventListener('click',()=>show(activeIndex-1));
    next?.addEventListener('click',()=>show(activeIndex+1));
    spotlight.addEventListener('pointerenter',()=>{paused=true;stop()});
    spotlight.addEventListener('pointerleave',()=>{paused=false;schedule()});
    spotlight.addEventListener('focusin',()=>{paused=true;stop()});
    spotlight.addEventListener('focusout',event=>{if(!spotlight.contains(event.relatedTarget)){paused=false;schedule()}});
    document.addEventListener('visibilitychange',schedule);
    slides.forEach((slide,index)=>{slide.hidden=index!==0;slide.inert=index!==0});
    schedule();
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
      const getPageSize=()=>window.matchMedia('(max-width: 600px)').matches?2:window.matchMedia('(max-width: 980px)').matches?3:window.matchMedia('(min-width: 1200px)').matches?5:4;
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
      track.addEventListener('click',async event=>{
        const countToggle=event.target.closest('[data-cart-count-toggle]');
        if(countToggle){
          const count=document.getElementById(countToggle.getAttribute('aria-controls'));
          if(!count)return;
          const expanded=countToggle.getAttribute('aria-expanded')==='true';
          countToggle.setAttribute('aria-expanded',String(!expanded));
          count.hidden=expanded;
          const productName=countToggle.closest('[data-product-card]')?.querySelector('.most-added-product-name')?.textContent.trim()||'product';
          countToggle.setAttribute('aria-label',`${expanded?'Show':'Hide'} how many ${productName} items shoppers have added to carts`);
          return;
        }
        const likeButton=event.target.closest('[data-product-like]');
        if(!likeButton||likeButton.disabled)return;
        likeButton.disabled=true;
        try{
          const response=await fetch(likeButton.dataset.url,{method:'POST',headers:{'X-CSRFToken':document.querySelector('meta[name="csrf-token"]')?.content||'',Accept:'application/json'}});
          const result=await response.json();
          if(!response.ok||!result.ok)throw new Error(result.message||'Like unavailable');
          likeButton.setAttribute('aria-pressed',String(result.liked));
          likeButton.dataset.liked=String(result.liked);
          likeButton.setAttribute('aria-label',`${result.liked?'Unlike':'Like'} ${likeButton.closest('[data-product-card]')?.querySelector('.most-added-product-name')?.textContent.trim()||'product'}`);
          const count=likeButton.querySelector('[data-like-count]');
          if(count)count.textContent=String(result.like_count);
          const card=likeButton.closest('.marquee-item');
          if(card)card.dataset.likeCount=String(result.like_count);
          if(status)status.textContent='Likes saved. Products are ordered by likes, then cart additions.';
          cards.sort((left,right)=>Number(right.dataset.likeCount||0)-Number(left.dataset.likeCount||0)||Number(right.dataset.cartCount||0)-Number(left.dataset.cartCount||0));
          track.replaceChildren(...cards);
          pageSize=0;
          buildPages();
        }catch(error){
          if(status)status.textContent=error.message||'Your like could not be saved. Please try again.';
        }finally{likeButton.disabled=false;}
      });
      window.addEventListener('fity:cart-add-count-updated',event=>{
        const productId=String(event.detail?.productId||'');
        const card=cards.find(item=>item.querySelector('[data-product-card]')?.dataset.productCard===productId);
        if(!card)return;
        const count=Math.max(0,Number(event.detail?.addCount||0));
        card.dataset.cartCount=String(count);
        const countText=card.querySelector('.most-added-cart-count');
        if(countText)countText.textContent=`${count} items added to carts`;
        cards.sort((left,right)=>Number(right.dataset.likeCount||0)-Number(left.dataset.likeCount||0)||Number(right.dataset.cartCount||0)-Number(left.dataset.cartCount||0));
        track.replaceChildren(...cards);
        pageSize=0;
        buildPages();
      });
      buildPages();
      window.addEventListener('resize',buildPages,{passive:true});
    }
  }

  const marquees=Array.from(document.querySelectorAll('.featured-showcase .product-marquee,.promo-showcase .product-marquee'));
  const updateMarquees=()=>marquees.forEach(marquee=>{
    const track=marquee.querySelector('.marquee-track');
    const firstGroup=track?.querySelector('.marquee-group:not([aria-hidden="true"])');
    if(!track||!firstGroup||!firstGroup.children.length)return;
    const interactiveCopies=marquee.closest('.featured-showcase');
    const makePointerInteractive=group=>{
      group.setAttribute('aria-hidden','true');
      if(!interactiveCopies){group.setAttribute('inert','');return}
      group.removeAttribute('inert');
      group.querySelectorAll('a,button,input,select,textarea,[tabindex]').forEach(element=>element.setAttribute('tabindex','-1'));
    };
    track.querySelectorAll('.marquee-group[aria-hidden="true"]').forEach(makePointerInteractive);
    marquee.classList.remove('marquee-static');
    let addedBatches=0;
    while(track.scrollWidth<marquee.clientWidth*1.4&&addedBatches<16){
      for(let copy=0;copy<4;copy++){
        const clone=firstGroup.cloneNode(true);
        makePointerInteractive(clone);
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

const showcaseReducedMotion=window.matchMedia('(prefers-reduced-motion: reduce)');
const showcaseVideoPreviews=[...document.querySelectorAll('[data-showcase-video-preview]')];
let showcaseVideoPreviewObserver=null;
function stopShowcaseVideoPreview(video){
  if(!video)return;
  video.pause();
  video.removeAttribute('data-playing');
  if(video.hasAttribute('src')){
    video.removeAttribute('src');
    video.load();
  }
}
function playShowcaseVideoPreview(video){
  if(!video||showcaseReducedMotion.matches||video.dataset.autoplay!=='1')return;
  const source=video.dataset.previewSrc;
  if(!source)return;
  video.defaultMuted=true;
  video.muted=true;
  video.playsInline=true;
  if(video.getAttribute('src')!==source){
    video.src=source;
    video.load();
  }
  video.autoplay=true;
  video.play().catch(error=>{
    if(error.name!=='NotAllowedError'&&error.name!=='AbortError')console.warn('Unable to autoplay the storefront video preview.',error);
  });
}
let showcaseVideoPreviewFallbackFrame=0;
function updateShowcaseVideoPreviewsInViewport(){
  if(showcaseVideoPreviewFallbackFrame)return;
  showcaseVideoPreviewFallbackFrame=requestAnimationFrame(()=>{
    showcaseVideoPreviewFallbackFrame=0;
    showcaseVideoPreviews.forEach(video=>{
      const bounds=video.getBoundingClientRect();
      if(bounds.bottom>=-180&&bounds.top<=window.innerHeight+180&&bounds.right>=-320&&bounds.left<=window.innerWidth+320){
        playShowcaseVideoPreview(video);
      }else stopShowcaseVideoPreview(video);
    });
  });
}
showcaseVideoPreviews.forEach(video=>video.addEventListener('playing',()=>video.setAttribute('data-playing','1')));
if(!showcaseReducedMotion.matches&&'IntersectionObserver' in window){
  showcaseVideoPreviewObserver=new IntersectionObserver(entries=>{
    entries.forEach(entry=>{
      if(entry.isIntersecting)playShowcaseVideoPreview(entry.target);
      else stopShowcaseVideoPreview(entry.target);
    });
  },{rootMargin:'180px 320px',threshold:.01});
  showcaseVideoPreviews.forEach(video=>showcaseVideoPreviewObserver.observe(video));
}else if(!showcaseReducedMotion.matches){
  window.addEventListener('scroll',updateShowcaseVideoPreviewsInViewport,{passive:true});
  window.addEventListener('resize',updateShowcaseVideoPreviewsInViewport,{passive:true});
  updateShowcaseVideoPreviewsInViewport();
}
document.addEventListener('click',event=>{
  const scrollButton=event.target.closest('[data-showcase-scroll]');
  if(scrollButton){
    const rail=scrollButton.closest('.media-showcase')?.querySelector('[data-showcase-rail]');
    if(rail)rail.scrollBy({left:Number(scrollButton.dataset.showcaseScroll)*Math.max(rail.clientWidth*.8,240),behavior:showcaseReducedMotion.matches?'auto':'smooth'});
    return;
  }
  const viewAll=event.target.closest('[data-showcase-view-all]');
  if(viewAll){
    const rail=document.getElementById(viewAll.getAttribute('aria-controls'));
    if(rail){const expanded=rail.classList.toggle('is-expanded');viewAll.setAttribute('aria-expanded',String(expanded));viewAll.textContent=expanded?'Show less media':'View all media';}
    return;
  }
  const close=event.target.closest('[data-media-player-close]');
  if(close){if(showcasePlayer?.open)showcasePlayer.close();return;}
  const minimize=event.target.closest('[data-media-player-minimize]');
  if(minimize&&showcasePlayer){
    const minimized=!showcasePlayer.classList.contains('is-minimized');
    const minimizedPanel=showcasePlayer.querySelector('[data-media-player-minimized]');
    if(minimized){
      showcasePlayer.classList.add('is-minimized');
      showcasePlayer.classList.remove('is-expanded');
      showcasePlayer.style.removeProperty('width');
      showcasePlayer.style.removeProperty('height');
      showcasePlayer.style.removeProperty('min-width');
      showcasePlayer.style.removeProperty('min-height');
      showcasePlayer.classList.remove('is-pinch-resizing');
      clearShowcasePlayerContent();
      if(showcasePlayerContent)showcasePlayerContent.hidden=true;
      if(minimizedPanel)minimizedPanel.hidden=false;
      const enlarge=showcasePlayer.querySelector('[data-media-player-enlarge]');
      if(enlarge){enlarge.setAttribute('aria-pressed','false');enlarge.setAttribute('aria-label','Enlarge media player');}
    }else{
      showcasePlayer.classList.remove('is-minimized');
      if(showcasePlayerContent)showcasePlayerContent.hidden=false;
      if(minimizedPanel)minimizedPanel.hidden=true;
      if(showcaseCurrentOpener?.isConnected)openShowcasePlayer(showcaseCurrentOpener,showcaseCurrentAutomatic);
    }
    minimize.setAttribute('aria-expanded',String(!minimized));
    minimize.setAttribute('aria-label',minimized?'Restore media':'Minimize media player');
    minimize.querySelector('path')?.setAttribute('d',minimized?'M12 5v14m-7-7h14':'M5 12h14');
    return;
  }
  const enlarge=event.target.closest('[data-media-player-enlarge]');
  if(enlarge&&showcasePlayer){
    showcasePlayer.style.removeProperty('width');
    showcasePlayer.style.removeProperty('height');
    showcasePlayer.style.removeProperty('min-width');
    showcasePlayer.style.removeProperty('min-height');
    showcasePlayer.classList.remove('is-pinch-resizing');
    const expanded=showcasePlayer.classList.toggle('is-expanded');
    enlarge.setAttribute('aria-pressed',String(expanded));
    enlarge.setAttribute('aria-label',expanded?'Restore media player size':'Enlarge media player');
    return;
  }
  const opener=event.target.closest('[data-showcase-media-open]');
  if(opener)openShowcasePlayer(opener,false);
});

const showcasePlayers=[...document.querySelectorAll('[data-media-showcase-player]')];
const showcasePlayer=showcasePlayers[0]||null;
showcasePlayers.slice(1).forEach(player=>player.remove());
if(showcasePlayer)document.body.append(showcasePlayer);
const showcasePlayerContent=showcasePlayer?.querySelector('[data-media-player-content]');
const showcasePlayerTitle=showcasePlayer?.querySelector('[data-media-player-title]');
const showcasePlayerMinimized=showcasePlayer?.querySelector('[data-media-player-minimized]');
let showcasePopupSeenInMemory=false;
let showcaseLastOpener=null;
let showcaseCurrentOpener=null;
let showcaseCurrentAutomatic=false;
let showcasePlayerManuallyPositioned=false;
let showcasePlayerPositionFrame=0;
const showcaseTouchPointers=new Map();
let showcasePinchStart=null;
let showcaseTouchDrag=null;
let showcaseTouchHoldTimer=0;

function hasSeenShowcasePopup(){
  return showcasePopupSeenInMemory;
}
function markShowcasePopupSeen(){
  showcasePopupSeenInMemory=true;
}
function positionShowcasePlayerAwayFromCards(){
  if(!showcasePlayer?.open||showcasePlayerManuallyPositioned)return;
  const playerRect=showcasePlayer.getBoundingClientRect();
  const margin=12;
  const positions=[
    {left:window.innerWidth-playerRect.width-margin,top:window.innerHeight-playerRect.height-margin},
    {left:margin,top:window.innerHeight-playerRect.height-margin},
    {left:window.innerWidth-playerRect.width-margin,top:margin},
    {left:margin,top:margin}
  ];
  const cards=[...document.querySelectorAll('.media-showcase-card')];
  const overlapArea=(position,rect)=>{
    const width=Math.max(0,Math.min(position.left+playerRect.width,rect.right)-Math.max(position.left,rect.left));
    const height=Math.max(0,Math.min(position.top+playerRect.height,rect.bottom)-Math.max(position.top,rect.top));
    return width*height;
  };
  const bestPosition=positions
    .map(position=>({
      ...position,
      overlap:cards.reduce((total,card)=>total+overlapArea(position,card.getBoundingClientRect()),0)
    }))
    .sort((first,second)=>first.overlap-second.overlap)[0];
  showcasePlayer.style.left=`${Math.max(margin,bestPosition.left)}px`;
  showcasePlayer.style.top=`${Math.max(margin,bestPosition.top)}px`;
  showcasePlayer.style.right='auto';
  showcasePlayer.style.bottom='auto';
}
function scheduleShowcasePlayerPosition(){
  if(!showcasePlayer?.open||showcasePlayerManuallyPositioned||showcasePlayerPositionFrame)return;
  showcasePlayerPositionFrame=requestAnimationFrame(()=>{
    showcasePlayerPositionFrame=0;
    positionShowcasePlayerAwayFromCards();
  });
}
function clearShowcaseTouchHoldTimer(){
  if(showcaseTouchHoldTimer){
    window.clearTimeout(showcaseTouchHoldTimer);
    showcaseTouchHoldTimer=0;
  }
}
function positionShowcasePlayerWithinViewport(left,top){
  if(!showcasePlayer)return;
  const margin=8;
  const maxLeft=Math.max(margin,window.innerWidth-showcasePlayer.offsetWidth-margin);
  const maxTop=Math.max(margin,window.innerHeight-showcasePlayer.offsetHeight-margin);
  showcasePlayer.style.left=`${Math.min(Math.max(margin,left),maxLeft)}px`;
  showcasePlayer.style.top=`${Math.min(Math.max(margin,top),maxTop)}px`;
  showcasePlayer.style.right='auto';
  showcasePlayer.style.bottom='auto';
}
function handleShowcasePinchPointer(event){
  if(event.pointerType!=='touch')return;
  if(event.type==='pointerdown'){
    if(!showcasePlayer?.open||showcasePlayer.classList.contains('is-minimized')||!showcasePlayer.contains(event.target))return;
    showcaseTouchPointers.set(event.pointerId,{x:event.clientX,y:event.clientY});
    const target=event.target instanceof Element?event.target:null;
    if(showcaseTouchPointers.size===1&&!target?.closest('button,a,input,select,textarea,[role="button"]')){
      const rect=showcasePlayer.getBoundingClientRect();
      showcaseTouchDrag={
        pointerId:event.pointerId,
        startX:event.clientX,
        startY:event.clientY,
        offsetX:event.clientX-rect.left,
        offsetY:event.clientY-rect.top,
        dragging:false
      };
      showcaseTouchHoldTimer=window.setTimeout(()=>{
        if(showcaseTouchDrag?.pointerId!==event.pointerId||showcaseTouchPointers.size!==1)return;
        showcaseTouchDrag.dragging=true;
        showcasePlayer.classList.add('is-dragging');
        try{showcasePlayer.setPointerCapture(event.pointerId);}catch(_){}
      },240);
    }
    if(showcaseTouchPointers.size===2){
      clearShowcaseTouchHoldTimer();
      showcaseTouchDrag=null;
      showcasePlayer.classList.remove('is-dragging');
      const pointerIds=[...showcaseTouchPointers.keys()];
      const [first,second]=pointerIds.map(pointerId=>showcaseTouchPointers.get(pointerId));
      const rect=showcasePlayer.getBoundingClientRect();
      showcasePinchStart={
        pointerIds,
        distance:Math.max(1,Math.hypot(second.x-first.x,second.y-first.y)),
        width:rect.width,
        height:rect.height,
        left:rect.left,
        top:rect.top
      };
      showcasePlayerManuallyPositioned=true;
      showcasePlayer.classList.remove('is-expanded');
      showcasePlayer.classList.add('is-pinch-resizing');
      showcasePlayer.style.minWidth='0';
      showcasePlayer.style.minHeight='0';
      const enlarge=showcasePlayer.querySelector('[data-media-player-enlarge]');
      if(enlarge){
        enlarge.setAttribute('aria-pressed','false');
        enlarge.setAttribute('aria-label','Enlarge media player');
      }
      event.preventDefault();
    }
    return;
  }
  if(!showcaseTouchPointers.has(event.pointerId))return;
  if(event.type==='pointermove'){
    showcaseTouchPointers.set(event.pointerId,{x:event.clientX,y:event.clientY});
    if(showcasePinchStart){
      const [firstId,secondId]=showcasePinchStart.pointerIds;
      const first=showcaseTouchPointers.get(firstId);
      const second=showcaseTouchPointers.get(secondId);
      if(!first||!second)return;
      const distance=Math.max(1,Math.hypot(second.x-first.x,second.y-first.y));
      const minWidth=Math.min(160,window.innerWidth-16);
      const minHeight=Math.min(112,window.innerHeight-16);
      const maxWidth=Math.max(minWidth,window.innerWidth-16);
      const maxHeight=Math.max(minHeight,window.innerHeight*.8);
      const minScale=Math.max(minWidth/showcasePinchStart.width,minHeight/showcasePinchStart.height);
      const maxScale=Math.max(minScale,Math.min(maxWidth/showcasePinchStart.width,maxHeight/showcasePinchStart.height));
      const scale=Math.min(maxScale,Math.max(minScale,distance/showcasePinchStart.distance));
      const width=showcasePinchStart.width*scale;
      const height=showcasePinchStart.height*scale;
      const margin=8;
      const centerX=showcasePinchStart.left+showcasePinchStart.width/2;
      const centerY=showcasePinchStart.top+showcasePinchStart.height/2;
      const maxLeft=Math.max(margin,window.innerWidth-width-margin);
      const maxTop=Math.max(margin,window.innerHeight-height-margin);
      showcasePlayer.style.width=`${width}px`;
      showcasePlayer.style.height=`${height}px`;
      showcasePlayer.style.left=`${Math.min(maxLeft,Math.max(margin,centerX-width/2))}px`;
      showcasePlayer.style.top=`${Math.min(maxTop,Math.max(margin,centerY-height/2))}px`;
      showcasePlayer.style.right='auto';
      showcasePlayer.style.bottom='auto';
      event.preventDefault();
      return;
    }
    if(showcaseTouchDrag?.pointerId===event.pointerId){
      const drag=showcaseTouchDrag;
      if(!drag.dragging){
        if(Math.hypot(event.clientX-drag.startX,event.clientY-drag.startY)>10){
          clearShowcaseTouchHoldTimer();
          showcaseTouchDrag=null;
        }
        return;
      }
      showcasePlayerManuallyPositioned=true;
      positionShowcasePlayerWithinViewport(event.clientX-drag.offsetX,event.clientY-drag.offsetY);
      event.preventDefault();
    }
    return;
  }
  showcaseTouchPointers.delete(event.pointerId);
  if(showcaseTouchDrag?.pointerId===event.pointerId){
    clearShowcaseTouchHoldTimer();
    showcaseTouchDrag=null;
    showcasePlayer?.classList.remove('is-dragging');
    try{
      if(showcasePlayer?.hasPointerCapture(event.pointerId))showcasePlayer.releasePointerCapture(event.pointerId);
    }catch(_){}
  }
  if(showcasePinchStart?.pointerIds.includes(event.pointerId)){
    showcasePinchStart=null;
    showcaseTouchPointers.clear();
    showcasePlayer?.classList.remove('is-pinch-resizing');
  }
}
function mediaPlayerError(message){
  if(showcasePlayerContent?.querySelector('.media-showcase-player-error'))return;
  const notice=document.createElement('p');
  notice.className='media-showcase-player-error';
  notice.setAttribute('role','alert');
  notice.textContent=message;
  showcasePlayerContent?.append(notice);
}
function clearShowcasePlayerContent(){
  if(!showcasePlayerContent)return;
  showcasePlayerContent.querySelectorAll('video').forEach(video=>{
    video.pause();
    video.removeAttribute('src');
    video.load();
  });
  showcasePlayerContent.querySelectorAll('iframe').forEach(frame=>{frame.src='about:blank';});
  showcasePlayerContent.replaceChildren();
}
function openShowcasePlayer(opener,automatic){
  if(!showcasePlayer||!showcasePlayerContent)return false;
  showcaseCurrentOpener=opener;
  showcaseCurrentAutomatic=automatic;
  const kind=opener.dataset.mediaKind;
  const title=opener.dataset.mediaTitle||'Featured media';
  const muted=kind==='video'||automatic||opener.dataset.mediaMuted==='1';
  const shouldAutoplay=kind==='video'&&opener.dataset.mediaAutoplay!=='0'&&!showcaseReducedMotion.matches;
  clearShowcasePlayerContent();
  if(showcasePlayerTitle)showcasePlayerTitle.textContent=title;
  showcasePlayer.classList.remove('is-minimized');
  showcasePlayerContent.hidden=false;
  if(showcasePlayerMinimized)showcasePlayerMinimized.hidden=true;
  const minimize=showcasePlayer.querySelector('[data-media-player-minimize]');
  if(minimize){minimize.setAttribute('aria-expanded','true');minimize.setAttribute('aria-label','Minimize media player');}
  const ratio=(opener.dataset.mediaAspectRatio||'').match(/^([\d.]+):([\d.]+)$/);
  if(ratio&&Number(ratio[1])>0&&Number(ratio[2])>0&&Number(ratio[1])<=1000&&Number(ratio[2])<=1000){
    showcasePlayerContent.style.setProperty('--media-player-ratio',`${ratio[1]} / ${ratio[2]}`);
  }else showcasePlayerContent.style.removeProperty('--media-player-ratio');

  if(kind==='video'){
    const embedUrl=opener.dataset.videoEmbed;
    if(embedUrl){
      try{
        const embed=new URL(embedUrl);
        const allowedHosts=['youtube.com','www.youtube.com','www.youtube-nocookie.com','player.vimeo.com'];
        if(!allowedHosts.includes(embed.hostname.toLowerCase()))throw new Error('Unsupported video embed host.');
        embed.searchParams.set('autoplay',shouldAutoplay?'1':'0');
        embed.searchParams.set('playsinline','1');
        if(muted){
          if(embed.hostname.toLowerCase().includes('vimeo.com'))embed.searchParams.set('muted','1');
          else embed.searchParams.set('mute','1');
        }
        const frame=document.createElement('iframe');
        frame.src=embed.href;
        frame.title=title;
        frame.allow='autoplay; fullscreen; picture-in-picture; encrypted-media';
        frame.allowFullscreen=true;
        frame.referrerPolicy='strict-origin-when-cross-origin';
        frame.setAttribute('sandbox','allow-scripts allow-same-origin allow-presentation');
        showcasePlayerContent.append(frame);
      }catch(error){
        console.warn('Unable to open the configured media showcase embed.',error);
        mediaPlayerError('This video could not be opened.');
      }
    }else if(opener.dataset.mediaSrc){
      const video=document.createElement('video');
      video.controls=true;
      video.autoplay=shouldAutoplay;
      video.defaultMuted=muted;
      video.muted=muted;
      video.playsInline=true;
      video.preload='metadata';
      video.setAttribute('aria-label',title);
      if(muted)video.setAttribute('muted','');
      if(opener.dataset.videoPoster)video.poster=opener.dataset.videoPoster;
      video.addEventListener('error',()=>{
        if(!showcasePlayerContent.contains(video))return;
        const code=video.error?.code;
        mediaPlayerError(code===3||code===4
          ?'This video format is not supported. Use an MP4 encoded with H.264, or a supported direct video URL.'
          :'This video could not be loaded. Check its URL and storage access.');
      },{once:true});
      video.addEventListener('playing',()=>showcasePlayerContent.querySelector('.media-showcase-player-error')?.remove());
      video.src=opener.dataset.mediaSrc;
      showcasePlayerContent.append(video);
      if(shouldAutoplay)video.play().catch(error=>{
        if(error.name==='NotAllowedError')mediaPlayerError('Autoplay was blocked by your browser. Press Play to start the video.');
        else console.warn('Unable to autoplay the selected media showcase video.',error);
      });
    }else{
      mediaPlayerError('This video does not have a playable source.');
    }
  }else if(kind==='image'||kind==='gif'){
    if(!opener.dataset.mediaSrc)mediaPlayerError('This media does not have an image source.');
    else{
      const image=document.createElement('img');
      image.src=opener.dataset.mediaSrc;
      image.alt=opener.dataset.mediaAlt||title;
      image.addEventListener('error',()=>{if(showcasePlayerContent.contains(image))mediaPlayerError('This media could not be loaded.');},{once:true});
      showcasePlayerContent.append(image);
    }
  }else{
    mediaPlayerError('This media format is not supported.');
  }
  if(!automatic)showcaseLastOpener=opener;
  if(!showcasePlayer.open)showcasePlayer.show();
  if(automatic)scheduleShowcasePlayerPosition();
  if(!automatic)showcasePlayer.querySelector('[data-media-player-close]')?.focus({preventScroll:true});
  if(automatic)markShowcasePopupSeen();
  return true;
}

if(showcasePlayer){
  showcasePlayer.addEventListener('close',()=>{
    markShowcasePopupSeen();
    clearShowcasePlayerContent();
    showcasePlayerContent?.removeAttribute('hidden');
    if(showcasePlayerMinimized)showcasePlayerMinimized.hidden=true;
    showcasePlayerContent?.style.removeProperty('--media-player-ratio');
    showcasePlayer.style.removeProperty('width');
    showcasePlayer.style.removeProperty('height');
    showcasePlayer.style.removeProperty('min-width');
    showcasePlayer.style.removeProperty('min-height');
    showcasePlayer.classList.remove('is-expanded','is-minimized');
    showcasePlayer.classList.remove('is-pinch-resizing','is-dragging');
    clearShowcaseTouchHoldTimer();
    showcaseTouchDrag=null;
    showcaseTouchPointers.clear();
    showcasePinchStart=null;
    if(showcaseLastOpener?.isConnected)showcaseLastOpener.focus();
    showcaseLastOpener=null;
    showcaseCurrentOpener=null;
    showcaseCurrentAutomatic=false;
  });
  showcasePlayer.addEventListener('cancel',markShowcasePopupSeen);

  const dragHandle=showcasePlayer.querySelector('.media-showcase-player-header');
  let activeDrag=null;
  dragHandle?.addEventListener('pointerdown',event=>{
    if(event.pointerType==='touch'||event.button!==0||event.target.closest('button'))return;
    showcasePlayerManuallyPositioned=true;
    const rect=showcasePlayer.getBoundingClientRect();
    activeDrag={pointerId:event.pointerId,offsetX:event.clientX-rect.left,offsetY:event.clientY-rect.top};
    dragHandle.setPointerCapture(event.pointerId);
    showcasePlayer.classList.add('is-dragging');
    event.preventDefault();
  });
  dragHandle?.addEventListener('pointermove',event=>{
    if(activeDrag?.pointerId!==event.pointerId)return;
    positionShowcasePlayerWithinViewport(event.clientX-activeDrag.offsetX,event.clientY-activeDrag.offsetY);
  });
  const stopDragging=event=>{
    if(activeDrag?.pointerId!==event.pointerId)return;
    activeDrag=null;
    showcasePlayer.classList.remove('is-dragging');
  };
  dragHandle?.addEventListener('pointerup',stopDragging);
  dragHandle?.addEventListener('pointercancel',stopDragging);
  window.addEventListener('resize',()=>{
    if(showcasePlayer.open&&showcasePlayer.style.left){
      const rect=showcasePlayer.getBoundingClientRect();
      positionShowcasePlayerWithinViewport(rect.left,rect.top);
    }
    scheduleShowcasePlayerPosition();
  },{passive:true});
  window.addEventListener('scroll',scheduleShowcasePlayerPosition,{passive:true});
  window.addEventListener('pointerdown',handleShowcasePinchPointer,{capture:true,passive:false});
  window.addEventListener('pointermove',handleShowcasePinchPointer,{capture:true,passive:false});
  window.addEventListener('pointerup',handleShowcasePinchPointer,true);
  window.addEventListener('pointercancel',handleShowcasePinchPointer,true);
}
if(showcasePlayer&&!showcaseReducedMotion.matches&&!hasSeenShowcasePopup()){
  const featuredOpener=document.querySelector('[data-showcase-featured] [data-showcase-media-open]');
  if(featuredOpener){
    const featuredCard=featuredOpener.closest('[data-showcase-featured]')||featuredOpener;
    if('IntersectionObserver' in window){
      const featuredObserver=new IntersectionObserver(entries=>{
        if(!entries.some(entry=>entry.isIntersecting))return;
        featuredObserver.disconnect();
        if(!showcasePlayer.open&&!hasSeenShowcasePopup()&&!showcaseReducedMotion.matches)openShowcasePlayer(featuredOpener,true);
      },{threshold:0.1});
      featuredObserver.observe(featuredCard);
    }else openShowcasePlayer(featuredOpener,true);
  }
}
if(showcaseReducedMotion.matches){
  document.querySelectorAll('.media-showcase-image-preview[data-media-kind="gif"]').forEach(preview=>{
    const poster=preview.dataset.mediaPoster;
    const image=preview.querySelector('img');
    if(!image)return;
    if(poster){image.src=poster;return;}
    const ratio=(preview.dataset.mediaAspectRatio||'').match(/^([\d.]+):([\d.]+)$/);
    if(ratio&&Number(ratio[1])>0&&Number(ratio[2])>0){
      preview.style.setProperty('--media-item-ratio',`${ratio[1]} / ${ratio[2]}`);
    }else if(image.naturalWidth&&image.naturalHeight){
      preview.style.setProperty('--media-item-ratio',`${image.naturalWidth} / ${image.naturalHeight}`);
    }
    const placeholder=document.createElement('span');
    placeholder.className='media-showcase-reduced-motion-placeholder';
    placeholder.textContent=preview.dataset.mediaTitle||'Animated media';
    image.replaceWith(placeholder);
  });
}

function setShowcaseMediaRatio(media){
  const videoPreview=media.closest('.media-showcase-video-preview');
  if(videoPreview){
    let width=Number(videoPreview.dataset.sourceWidth)||0;
    let height=Number(videoPreview.dataset.sourceHeight)||0;
    const match=videoPreview.dataset.ratioKnown==='1'?(videoPreview.dataset.aspectRatio||'').match(/^([\d.]+):([\d.]+)$/):null;
    if(!(width>0&&height>0)&&match){width=Number(match[1]);height=Number(match[2]);}
    if(!(width>0&&height>0)){width=media instanceof HTMLVideoElement?media.videoWidth:media.naturalWidth;height=media instanceof HTMLVideoElement?media.videoHeight:media.naturalHeight;}
    if(width>0&&height>0){videoPreview.style.setProperty('--media-item-ratio',`${width} / ${height}`);videoPreview.style.setProperty('--media-preview-max-width',`${360*width/height}px`);}
    return;
  }
  const preview=media.closest('.media-showcase-image-preview');
  const mode=media.dataset.presentationMode||'original';
  const ratioText=media.dataset.aspectRatio||'';
  const match=ratioText.match(/^([\d.]+):([\d.]+)$/);
  if(mode!=='original'&&match){
    const width=Number(match[1]),height=Number(match[2]);
    if(width>0&&height>0&&width<=1000&&height<=1000){
      media.style.setProperty('--media-item-ratio',`${width} / ${height}`);
      preview?.style.setProperty('--media-item-ratio',`${width} / ${height}`);
    }
    return;
  }
  const width=media instanceof HTMLVideoElement?media.videoWidth:media.naturalWidth;
  const height=media instanceof HTMLVideoElement?media.videoHeight:media.naturalHeight;
  if(width>0&&height>0){
    media.style.setProperty('--media-item-ratio',`${width} / ${height}`);
    preview?.style.setProperty('--media-item-ratio',`${width} / ${height}`);
  }
}
document.querySelectorAll('.media-showcase video,.media-showcase img').forEach(media=>{
  const refresh=()=>setShowcaseMediaRatio(media);
  media.addEventListener(media instanceof HTMLVideoElement?'loadedmetadata':'load',refresh,{once:false});
  if(media instanceof HTMLVideoElement){if(media.readyState>=1)refresh();}
  else if(media.complete)refresh();
});
if('IntersectionObserver'in window){
  const showcaseMetadataObserver=new IntersectionObserver(entries=>entries.forEach(entry=>{
    if(!entry.isIntersecting)return;
    const preview=entry.target;
    showcaseMetadataObserver.unobserve(preview);
    const probe=document.createElement('video');
    probe.preload='metadata';
    probe.muted=true;
    probe.src=preview.dataset.videoMetaSrc;
    probe.addEventListener('loadedmetadata',()=>{
      if(probe.videoWidth&&probe.videoHeight){
        preview.style.setProperty('--media-item-ratio',`${probe.videoWidth} / ${probe.videoHeight}`);
        preview.style.setProperty('--media-preview-max-width',`${360*probe.videoWidth/probe.videoHeight}px`);
      }
      probe.removeAttribute('src');
      probe.load();
    },{once:true});
    probe.addEventListener('error',()=>probe.removeAttribute('src'),{once:true});
  }),{rootMargin:'160px'});
  document.querySelectorAll('.media-showcase-video-preview[data-video-meta-src]').forEach(preview=>showcaseMetadataObserver.observe(preview));
}

function activateProductInfoTab(tab,{focus=false}={}){
  const tablist=tab.closest('[role="tablist"]');
  const detail=tablist?.closest('.product-information-card');
  if(!tablist||!detail)return;
  const tabs=[...tablist.querySelectorAll('[role="tab"]')];
  tabs.forEach(item=>{
    const selected=item===tab;
    item.setAttribute('aria-selected',String(selected));
    item.tabIndex=selected?0:-1;
    const panel=document.getElementById(item.getAttribute('aria-controls'));
    if(panel&&detail.contains(panel))panel.hidden=!selected;
  });
  if(focus)tab.focus();
}
document.addEventListener('click',event=>{
  const tab=event.target.closest('.product-information-tabs [role="tab"]');
  if(tab)activateProductInfoTab(tab);
});
document.addEventListener('keydown',event=>{
  const tab=event.target.closest('.product-information-tabs [role="tab"]');
  if(!tab)return;
  const tabs=[...tab.closest('[role="tablist"]').querySelectorAll('[role="tab"]')];
  const index=tabs.indexOf(tab);
  let next=index;
  if(event.key==='ArrowRight')next=(index+1)%tabs.length;
  else if(event.key==='ArrowLeft')next=(index-1+tabs.length)%tabs.length;
  else if(event.key==='Home')next=0;
  else if(event.key==='End')next=tabs.length-1;
  else return;
  event.preventDefault();
  activateProductInfoTab(tabs[next],{focus:true});
});
